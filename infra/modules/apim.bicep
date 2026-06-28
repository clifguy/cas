// CAS cloud deployment — API Management facade module.
//
// Provisions the public edge for SAGE in the cloud deployment profile
// (CAS-ADR-042): an API Management service fronting SAGE's REST and MCP
// surfaces. The facade validates Entra-issued JWTs (one issuer, one audience,
// uniform across every surface — the REST API, the ordinary MCP mount, and the
// maintenance mount alike) on the catch-all pipeline. The two unauthenticated
// surfaces — the MCP OAuth discovery doc and the /health liveness probe — are
// served by their own dedicated GET operations, whose operation-scoped policies
// omit <base /> so they never reach the catch-all's validate-jwt. The CAS BFF
// does not go through the facade — it uses the container ingress directly.
//
// Resource-group scoped (the Bicep default): the orchestrator deploys it with
// scope: rg. The backend hostname and the SAGE audience arrive as parameters,
// resolved when the SAGE container app and the Entra registration are concrete;
// the issuing tenant is derived from the deployment context. The inbound and
// operation policies are authored as versioned XML under infra/policies/ and
// loaded at compile time. Routing the unauthenticated surfaces by dedicated
// operation (a literal urlTemplate), rather than by a path-string <when>
// condition, keeps the policies free of an inline quoted string literal — the
// encoding the loadTextContent -> ARM -> APIM round-trip double-encodes.

@description('Azure region for the API Management service.')
param location string

@description('Short environment name, e.g. prod. Used in resource naming.')
param environmentName string

@description('Tags applied to the API Management service.')
param tags object

@description('Public hostname of the SAGE container app the facade routes to.')
param sageBackendHostname string

@description('SAGE resource-server audience the JWT policy validates (api://<app-id>).')
param sageAudience string

@description('Publisher email for the API Management service (administrative contact).')
param publisherEmail string

@description('Publisher organization name for the API Management service.')
param publisherName string = 'CAS Operations'

@description('SKU of the API Management service. Consumption is serverless and scale-to-zero.')
@allowed([
  'Consumption'
  'Developer'
  'BasicV2'
  'StandardV2'
])
param apimSku string = 'Consumption'

@description('Custom domain hostname bound to the gateway (e.g. sage.<base-domain>), served with the owned wildcard certificate.')
param sageCustomDomain string

@description('Resource id of the user-assigned managed identity APIM uses to read the custom-domain certificate from Key Vault.')
param sageIdentityId string

@description('Client id of that managed identity — the Key Vault GET principal for the custom-domain certificate.')
param sageIdentityClientId string

@description('Versionless Key Vault secret URL of the wildcard certificate. Versionless so the binding follows certificate rotation.')
param tlsCertSecretUri string

// The Consumption SKU is serverless and takes capacity 0; the classic and v2
// SKUs take a unit count. One unit is enough for this single-edge facade.
var apimCapacity = apimSku == 'Consumption' ? 0 : 1

// API Management gateway hostnames are globally unique; derive a stable name
// from the resource group id rather than risk a collision on a fixed name.
var apimName = 'apim-${environmentName}-${uniqueString(resourceGroup().id)}'

// The public edge. External (no VNet integration): it is the front door, and
// the foundation's ACA environment already exposes public ingress.
resource apimService 'Microsoft.ApiManagement/service@2022-08-01' = {
  name: apimName
  location: location
  tags: tags
  sku: {
    name: apimSku
    capacity: apimCapacity
  }
  // The user-assigned identity APIM authenticates to Key Vault with to read the
  // custom-domain certificate. It reuses the SAGE identity (already granted Key
  // Vault Certificate User on the vault); the vault has no firewall, so the
  // user-assigned path is supported on the Consumption SKU.
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${sageIdentityId}': {}
    }
  }
  properties: {
    publisherEmail: publisherEmail
    publisherName: publisherName
    virtualNetworkType: 'None'
    // The `sage` custom domain on the gateway endpoint — the only endpoint the
    // Consumption SKU exposes — served with the owned wildcard certificate from
    // Key Vault. keyVaultId is the versionless secret URL so the binding follows
    // certificate rotation; the managed identity's client id authorizes the read.
    hostnameConfigurations: [
      {
        type: 'Proxy'
        hostName: sageCustomDomain
        certificateSource: 'KeyVault'
        keyVaultId: tlsCertSecretUri
        identityClientId: sageIdentityClientId
        defaultSslBinding: true
      }
    ]
  }
}

// Named values carry the environment-specific coordinates the versioned policy
// XML references as {{...}} tokens. The tenant is derived from the deployment
// context; the audience is supplied; the resource URL is the gateway's own
// public address (the resource_metadata location advertised to MCP clients).
resource entraTenantIdNamedValue 'Microsoft.ApiManagement/service/namedValues@2022-08-01' = {
  parent: apimService
  name: 'entra-tenant-id'
  properties: {
    displayName: 'entra-tenant-id'
    value: subscription().tenantId
    secret: false
  }
}

resource sageAudienceNamedValue 'Microsoft.ApiManagement/service/namedValues@2022-08-01' = {
  parent: apimService
  name: 'sage-audience'
  properties: {
    displayName: 'sage-audience'
    value: sageAudience
    secret: false
  }
}

resource sageResourceUrlNamedValue 'Microsoft.ApiManagement/service/namedValues@2022-08-01' = {
  parent: apimService
  name: 'sage-resource-url'
  properties: {
    displayName: 'sage-resource-url'
    value: apimService.properties.gatewayUrl
    secret: false
  }
}

// The SAGE backend the facade routes to. The hostname is resolved at deploy
// time; certificate chain and name are validated on the upstream call.
resource sageBackend 'Microsoft.ApiManagement/service/backends@2022-08-01' = {
  parent: apimService
  name: 'sage-backend'
  properties: {
    title: 'SAGE'
    protocol: 'http'
    url: 'https://${sageBackendHostname}'
    tls: {
      validateCertificateChain: true
      validateCertificateName: true
    }
  }
}

// The SAGE API at the gateway root. Authorization is the Entra JWT (no APIM
// subscription key), so subscriptionRequired is false. A catch-all operation
// per HTTP method forwards every method and path; the inbound policy routes
// forwarded requests to the backend above (set-backend-service), gates access
// uniformly across every surface, and serves discovery.
resource sageApi 'Microsoft.ApiManagement/service/apis@2022-08-01' = {
  parent: apimService
  name: 'sage'
  properties: {
    displayName: 'SAGE'
    path: ''
    protocols: [
      'https'
    ]
    subscriptionRequired: false
  }
}

// APIM does not honor a wildcard ('*') HTTP method on an operation declared
// through ARM — the operation deploys and shows in the portal, but no incoming
// request ever matches it, so the gateway answers its generic 404 on every
// path. The working catch-all is one operation per explicit method, each with
// the '/{*path}' wildcard template and its declared 'path' parameter. The set
// covers SAGE's REST verbs and the MCP Streamable-HTTP transport (POST, GET for
// the SSE stream, DELETE for session teardown); HEAD and OPTIONS cover probes
// and preflight.
var sageHttpMethods = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS']

resource sageApiCatchAll 'Microsoft.ApiManagement/service/apis/operations@2022-08-01' = [
  for method in sageHttpMethods: {
    parent: sageApi
    name: 'catch-all-${toLower(method)}'
    properties: {
      displayName: 'Catch-all ${method}'
      method: method
      urlTemplate: '/{*path}'
      templateParameters: [
        {
          name: 'path'
          type: 'string'
          required: true
        }
      ]
    }
  }
]

// The OAuth discovery doc and /health are served by dedicated GET operations
// rather than a path-string <when> condition in the catch-all policy. APIM
// routes a literal-path operation ahead of the '/{*path}' catch-all, and the
// operation-scoped policies below omit <base /> so neither reaches validate-jwt
// — both answer 200 unauthenticated. This is the round-trip-safe replacement for
// the inline quoted literal the loadTextContent -> ARM -> APIM pipeline
// double-encoded (&quot; became &amp;quot;), which broke the discovery exemption
// and 401'd every path.
resource sageDiscoveryOperation 'Microsoft.ApiManagement/service/apis/operations@2022-08-01' = {
  parent: sageApi
  name: 'oauth-protected-resource'
  properties: {
    displayName: 'OAuth protected-resource metadata'
    method: 'GET'
    urlTemplate: '/.well-known/oauth-protected-resource'
    templateParameters: []
  }
}

resource sageHealthOperation 'Microsoft.ApiManagement/service/apis/operations@2022-08-01' = {
  parent: sageApi
  name: 'health'
  properties: {
    displayName: 'Liveness probe'
    method: 'GET'
    urlTemplate: '/health'
    templateParameters: []
  }
}

// The discovery operation serves the protected-resource-metadata document
// directly (return-response); the /health operation routes to the backend
// unauthenticated. Both operation policies omit <base /> in <inbound>, so the
// catch-all's validate-jwt does not run for them. The /health passthrough names
// the backend by id, so it depends on the backend existing first.
resource sageDiscoveryOperationPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2022-08-01' = {
  parent: sageDiscoveryOperation
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../policies/sage-discovery-operation-policy.xml')
  }
}

resource sageHealthOperationPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2022-08-01' = {
  parent: sageHealthOperation
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../policies/sage-health-operation-policy.xml')
  }
  dependsOn: [
    sageBackend
  ]
}

// The catch-all inbound policy — validate-jwt then route-to-backend — authored
// as versioned XML. It names the backend by id (set-backend-service), so it
// depends on the backend existing first.
resource sageApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2022-08-01' = {
  parent: sageApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../policies/sage-api-policy.xml')
  }
  dependsOn: [
    sageBackend
  ]
}

@description('Public gateway URL of the API Management facade.')
output apimGatewayUrl string = apimService.properties.gatewayUrl

@description('Name of the API Management service.')
output apimServiceName string = apimService.name
