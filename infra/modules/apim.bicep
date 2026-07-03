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

@description('Application (client) id of the pre-provisioned public MCP client (auth-code + PKCE, no secret) that the DCR-compatibility facade /register operation echoes back (CAS-ADR-042).')
param mcpClientId string

@description('Public URL of the CAS app a tokenless browser is redirected to from the SAGE edge — the cas custom-domain URL. Tenant-agnostic — the orchestrator supplies it; no hostname is baked into the module.')
param casAppUrl string

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

@description('Resource id of the Log Analytics workspace the gateway routes its diagnostic logs and metrics to (the foundation workspace).')
param logAnalyticsWorkspaceId string

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

// Route the gateway's own diagnostic logs and metrics to the foundation Log
// Analytics workspace (CAS-ADR-042). GatewayLogs entries — request outcomes,
// validate-jwt rejects, CORS preflight, backend latency — land in the workspace's
// dedicated ApiManagementGatewayLogs table (logAnalyticsDestinationType:
// 'Dedicated'), not the legacy consolidated AzureDiagnostics table. Retention is
// the workspace's own concern (no per-setting retentionPolicy; the override is
// deprecated for Log Analytics destinations).
//
// This resource only *routes* logs APIM has been told to emit. Azure Monitor
// platform metrics emit automatically, but APIM's own gateway request/response
// logs require the internal Diagnostic + Logger below to be emitted at all —
// without them GatewayLogs is enabled-but-empty (metrics flow, logs do not).
resource apimDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'apim-to-log-analytics'
  scope: apimService
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        category: 'GatewayLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

// The Azure-Monitor logger: the piece the ARM-level diagnosticSettings above
// cannot substitute for. It tells APIM to emit its gateway request logs to Azure
// Monitor — no Application Insights, so no instrumentation key, no credentials,
// and no target resource id (those belong only to applicationInsights /
// azureEventHub loggers). isBuffered is the default; named for the emit target.
resource apimAzureMonitorLogger 'Microsoft.ApiManagement/service/loggers@2022-08-01' = {
  parent: apimService
  name: 'azuremonitor'
  properties: {
    loggerType: 'azureMonitor'
    isBuffered: true
  }
}

// The service-level diagnostic that binds that logger. Its instance name must be
// the Azure-Monitor-reserved 'azuremonitor'. Sampling is pinned to 100% (with
// alwaysLog: 'allErrors' so failures bypass sampling) so every gateway request
// produces a log row — the guarantee the live ApiManagementGatewayLogs check
// depends on. metrics / httpCorrelationProtocol are Application-Insights-only and
// are omitted.
resource apimAzureMonitorDiagnostic 'Microsoft.ApiManagement/service/diagnostics@2022-08-01' = {
  parent: apimService
  name: 'azuremonitor'
  properties: {
    loggerId: apimAzureMonitorLogger.id
    alwaysLog: 'allErrors'
    sampling: {
      samplingType: 'fixed'
      percentage: 100
    }
  }
}

// Named values carry the environment-specific coordinates the versioned policy
// XML references as {{...}} tokens. The tenant is derived from the deployment
// context; the audience is supplied; the resource URL is the sage custom
// domain (the resource_metadata location advertised to MCP clients).
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

// The same resource's bare application-id GUID. A v2.0 access token (the SAGE
// app registration runs at requestedAccessTokenVersion 2, which the v2 issuer
// both validators require also implies) carries the bare GUID as its 'aud',
// not the api://<app-id> URI. The JWT policy accepts both forms, so the
// audience is reconciled without a second parameter -- the GUID is the URI's
// own suffix.
resource sageAudienceGuidNamedValue 'Microsoft.ApiManagement/service/namedValues@2022-08-01' = {
  parent: apimService
  name: 'sage-audience-guid'
  properties: {
    displayName: 'sage-audience-guid'
    value: replace(sageAudience, 'api://', '')
    secret: false
  }
}

// The public identity of the SAGE resource: the custom domain, NEVER the
// gateway's default *.azure-api.net address. An MCP client validates that the
// protected-resource metadata's `resource` matches the server URL it connected
// to (RFC 9728 confused-deputy protection -- the reference client throws
// "Protected resource ... does not match expected" on an origin mismatch), and
// composes its RFC 8707 `resource` authorize parameter from it, which Entra
// requires to be consistent with the requested scope (AADSTS9010010 otherwise).
// Advertising the internal gateway host breaks both; verified live on cor-prod.
resource sageResourceUrlNamedValue 'Microsoft.ApiManagement/service/namedValues@2022-08-01' = {
  parent: apimService
  name: 'sage-resource-url'
  properties: {
    displayName: 'sage-resource-url'
    value: 'https://${sageCustomDomain}'
    secret: false
  }
}

// The pre-provisioned public MCP client id the DCR-compatibility facade's
// /register operation echoes back on every registration attempt (CAS-ADR-042).
// Not a secret -- a public client (auth-code + PKCE) carries none -- but
// supplied as a parameter rather than a literal so no identity GUID lives in
// the module.
resource mcpClientIdNamedValue 'Microsoft.ApiManagement/service/namedValues@2022-08-01' = {
  parent: apimService
  name: 'mcp-client-id'
  properties: {
    displayName: 'mcp-client-id'
    value: mcpClientId
    secret: false
  }
}

// The CAS app a tokenless human browser is redirected to from the SAGE edge. The
// catch-all policy's <on-error> 401 branch 302s an Accept: text/html request here,
// while machine clients keep the protected-resource challenge. Supplied by the
// orchestrator (the cas custom-domain URL), so the policy carries no hardcoded host.
resource casAppUrlNamedValue 'Microsoft.ApiManagement/service/namedValues@2022-08-01' = {
  parent: apimService
  name: 'cas-app-url'
  properties: {
    displayName: 'cas-app-url'
    value: casAppUrl
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
// covers SAGE's REST verbs and the MCP Streamable HTTP transport (POST for
// JSON-RPC, GET and DELETE for the transport's optional stream and session
// legs); HEAD and OPTIONS cover probes and preflight.
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

// RFC 9728 path-inserted protected-resource metadata, one operation per MCP
// mount. Each document's `resource` is the path-carrying mount URI -- the form
// that survives a client's URL normalization byte-identically and is
// registered as an Entra identifier URI, so the client's RFC 8707 resource
// parameter clears Entra's byte-for-byte match (AADSTS9010010 otherwise; the
// bare host normalizes to a trailing-slash form Entra can neither match nor
// register). The catch-all policy's 401 challenge steers each mount's clients
// here. The mount list mirrors the uvicorn MCP mounts, the same protocol
// constant the Entra bootstrap registers identifier URIs for.
resource sageDiscoveryMcpOperation 'Microsoft.ApiManagement/service/apis/operations@2022-08-01' = {
  parent: sageApi
  name: 'oauth-protected-resource-mcp'
  properties: {
    displayName: 'OAuth protected-resource metadata (mcp mount)'
    method: 'GET'
    urlTemplate: '/.well-known/oauth-protected-resource/mcp'
    templateParameters: []
  }
}

resource sageDiscoveryMcpAdminOperation 'Microsoft.ApiManagement/service/apis/operations@2022-08-01' = {
  parent: sageApi
  name: 'oauth-protected-resource-mcp-admin'
  properties: {
    displayName: 'OAuth protected-resource metadata (mcp_admin mount)'
    method: 'GET'
    urlTemplate: '/.well-known/oauth-protected-resource/mcp_admin'
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

// The DCR-compatibility facade (CAS-ADR-042): two more dedicated,
// unauthenticated operations, the same round-trip-safe shape as discovery and
// /health. Entra offers no Dynamic Client Registration (RFC 7591); a
// standards-default MCP client's discovery-then-register leg otherwise
// dead-ends against Entra's real (registration-less) authorization-server
// metadata. The authorization-server metadata operation serves Entra's real
// authorize/token/JWKS endpoints plus a registration_endpoint pointing at
// /register; /register answers every registration attempt with the single
// pre-provisioned public client id. Only this leg is intercepted -- the
// authorize and token endpoints in the served metadata are Entra's own, so the
// browser redirect and token exchange never traverse the facade.
resource sageAuthorizationServerOperation 'Microsoft.ApiManagement/service/apis/operations@2022-08-01' = {
  parent: sageApi
  name: 'oauth-authorization-server'
  properties: {
    displayName: 'OAuth authorization-server metadata'
    method: 'GET'
    urlTemplate: '/.well-known/oauth-authorization-server'
    templateParameters: []
  }
}

resource sageRegisterOperation 'Microsoft.ApiManagement/service/apis/operations@2022-08-01' = {
  parent: sageApi
  name: 'register'
  properties: {
    displayName: 'DCR-compatibility static registration'
    method: 'POST'
    urlTemplate: '/register'
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

resource sageDiscoveryMcpOperationPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2022-08-01' = {
  parent: sageDiscoveryMcpOperation
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../policies/sage-discovery-mcp-operation-policy.xml')
  }
}

resource sageDiscoveryMcpAdminOperationPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2022-08-01' = {
  parent: sageDiscoveryMcpAdminOperation
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../policies/sage-discovery-mcp-admin-operation-policy.xml')
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

resource sageAuthorizationServerOperationPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2022-08-01' = {
  parent: sageAuthorizationServerOperation
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../policies/sage-authorization-server-operation-policy.xml')
  }
}

resource sageRegisterOperationPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2022-08-01' = {
  parent: sageRegisterOperation
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../policies/sage-register-operation-policy.xml')
  }
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
