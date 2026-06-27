// CAS cloud deployment — container-apps module.
//
// Declares the two Azure Container Apps the cloud deployment profile
// (CAS-ADR-042) runs: SAGE (the infra server) and the CAS BFF (the
// application-facing tier). Each app runs as its own user-assigned managed
// identity, pulls its image from the container registry by that identity (an
// AcrPull grant, never a stored registry credential), and reads its cloud
// configuration from a mounted YAML file the orchestrator assembles from the
// hosting modules' outputs. No secret value is carried in the image, the
// template, or the environment: the confidential credentials (the hosted
// abstraction key, the Postgres token, the BFF client secret) resolve from Key
// Vault or via the managed identity at runtime.
//
// Ingress posture mirrors the facade decision: SAGE takes external container
// ingress and the API Management facade routes to its resulting FQDN (exposed
// as an output so the orchestrator resolves the facade backend from a real
// value); the BFF takes external container ingress directly and attaches its
// custom domain via the environment certificate the custom-domains module
// imported.
//
// Resource-group scoped (the Bicep default): the orchestrator deploys it with
// scope: rg, composing the foundation, identity, Key Vault, Postgres, and
// custom-domains module outputs.

@description('Azure region for the container apps.')
param location string

@description('Short environment name, e.g. prod. Used in resource naming.')
param environmentName string

@description('Tags applied to the container apps.')
param tags object

@description('Resource id of the Azure Container Apps environment the apps deploy into.')
param acaEnvironmentId string

@description('Login server host of the Azure Container Registry the images are pulled from.')
param acrLoginServer string

@description('Name of the Azure Container Registry (referenced existing to scope the AcrPull grants).')
param acrName string

@description('Immutable image tag both apps pin to ({version}-{short-sha}); never `latest`.')
param imageTag string

@description('Resource id of the SAGE managed identity the SAGE app runs as.')
param sageIdentityId string

@description('Client id of the SAGE managed identity (runtime token acquisition).')
param sageIdentityClientId string

@description('Principal id of the SAGE managed identity (granted AcrPull).')
param sageIdentityPrincipalId string

@description('Resource id of the CAS BFF managed identity the BFF app runs as.')
param bffIdentityId string

@description('Client id of the CAS BFF managed identity (runtime token acquisition).')
param bffIdentityClientId string

@description('Principal id of the CAS BFF managed identity (granted AcrPull).')
param bffIdentityPrincipalId string

@description('Data-plane URI of the Key Vault the cloud profile reads its secrets from.')
param keyVaultUri string

@description('Canonical Key Vault secret name the BFF confidential-client secret is loaded under (single-sourced by the keyvault module).')
param bffClientSecretName string

@description('Fully qualified domain name of the managed Postgres server.')
param postgresServerFqdn string

@description('Name of the database SAGE and the BFF session store connect to.')
param postgresDatabaseName string

@description('SAGE resource-server audience (api://<app-id>) — the token audience and the BFF on-behalf-of scope resource.')
param sageAudience string

@description('Application (client) id of the CAS BFF confidential client registration.')
param bffOidcClientId string

@description('Custom domain hostname of the APIM-fronted SAGE edge the BFF reaches server-side.')
param sageHostname string

@description('Custom domain hostname bound to the CAS BFF container ingress.')
param casHostname string

@description('Resource id of the environment wildcard certificate the BFF custom domain binds.')
param casCertificateId string

@description('Claude model identifier the hosted abstraction provider generates abstracts with.')
param abstractionModel string = 'claude-haiku-4-5'

@description('Tenant id that issues the tokens the cloud profile validates.')
param tenantId string = subscription().tenantId

@description('Microsoft Graph site id of the SharePoint site hosting the cloud vault tree (the document-store vault-source binding, CAS-ADR-043). Empty leaves the coordinate unset until the runbook resolves it.')
param sharepointSiteId string = ''

@description('Microsoft Graph drive id of the SharePoint document library holding the cloud vault tree (CAS-ADR-043).')
param sharepointDriveId string = ''

@description('Folder path within the SharePoint document library the vault tree is rooted at (CAS-ADR-043).')
param vaultSourceRootPath string = 'vaults'

// Built-in Azure role: AcrPull (data-plane image pull). A public, fixed
// constant — not an identity coordinate.
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d' // gitleaks:allow public role id

// In the cloud profile each app authenticates to Postgres as itself: the libpq
// user is the app's managed-identity name (the Entra principal the access token
// represents), derived from the identity resource id.
var sageDbUser = last(split(sageIdentityId, '/'))
var bffDbUser = last(split(bffIdentityId, '/'))

// Versionless Key Vault secret URL of the BFF confidential-client secret. The
// secret value is loaded out of band by the documented operator step; the BFF
// reads it at runtime via its managed identity, so no secret is carried here.
var bffClientSecretUri = '${keyVaultUri}secrets/${bffClientSecretName}'

// The mounted cloud config (CAS-ADR-042). Assembled from the hosting modules'
// outputs as YAML and projected into each app as a file the runtime loads
// through SAGE_CONFIG_PATH. Non-secret coordinates only; profile/storage/auth
// keys must stay a subset of docs/fs/sage/sage_core_config.schema.json (the
// container-env <-> config-schema drift guard enforces this). The two configs
// differ only in postgres.user — each app connects as its own identity.
var sageConfigLines = [
  'profile: cloud'
  'storage_backend: postgres'
  // CAS-ADR-043: the cloud profile persists each vault's configuration
  // declaration to a SharePoint document library over Microsoft Graph, so a
  // vault survives the stateless compute's restart. This removes the prior
  // reliance on the ephemeral local vault root: the durable vault-source seam is
  // now the document store, not the container filesystem.
  'vault_source_backend: document_store'
  'abstraction:'
  '  provider: anthropic'
  '  model: ${abstractionModel}'
  'postgres:'
  '  host: ${postgresServerFqdn}'
  '  port: 5432'
  '  database: ${postgresDatabaseName}'
  '  user: ${sageDbUser}'
  '  sslmode: require'
  'document_store:'
  '  site_id: ${sharepointSiteId}'
  '  drive_id: ${sharepointDriveId}'
  '  root_path: ${vaultSourceRootPath}'
  'auth:'
  '  enabled: true'
  '  tenant_id: ${tenantId}'
  '  audience: ${sageAudience}'
]
var bffConfigLines = [
  'profile: cloud'
  'storage_backend: postgres'
  'abstraction:'
  '  provider: anthropic'
  '  model: ${abstractionModel}'
  'postgres:'
  '  host: ${postgresServerFqdn}'
  '  port: 5432'
  '  database: ${postgresDatabaseName}'
  '  user: ${bffDbUser}'
  '  sslmode: require'
  'auth:'
  '  enabled: true'
  '  tenant_id: ${tenantId}'
  '  audience: ${sageAudience}'
]
var sageConfigYaml = join(sageConfigLines, '\n')
var bffConfigYaml = join(bffConfigLines, '\n')

// Where each app's config file is projected and the path the runtime reads.
var sageConfigMountPath = '/etc/sage'
var bffConfigMountPath = '/etc/cas'
var configFileName = 'config.cloud.yaml'

// The registry, referenced existing so the AcrPull grants scope to it without
// re-declaring (and clobbering) the foundation's registry.
resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: acrName
}

// AcrPull for each app identity, scoped to the registry — the running app pulls
// its image with no stored credential. Mirrors the Key Vault module's grant
// pattern (guid name, subscription-scoped role-definition id, ServicePrincipal).
resource sageAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, sageIdentityPrincipalId, acrPullRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: sageIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource bffAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, bffIdentityPrincipalId, acrPullRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: bffIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// SAGE — external container ingress on its service port; the APIM facade routes
// to the FQDN this app exposes. Runs as the SAGE identity; reads its config from
// the mounted YAML and its secrets from Key Vault via that identity.
resource sageApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-sage-${environmentName}'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${sageIdentityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: acaEnvironmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
      }
      registries: [
        {
          server: acrLoginServer
          identity: sageIdentityId
        }
      ]
      secrets: [
        {
          name: 'sage-cloud-config'
          value: sageConfigYaml
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'sage'
          image: '${acrLoginServer}/sage:${imageTag}'
          env: [
            {
              name: 'SAGE_CONFIG_PATH'
              value: '${sageConfigMountPath}/${configFileName}'
            }
            {
              name: 'SAGE_KEY_VAULT_URI'
              value: keyVaultUri
            }
            {
              name: 'AZURE_CLIENT_ID'
              value: sageIdentityClientId
            }
          ]
          volumeMounts: [
            {
              volumeName: 'config'
              mountPath: sageConfigMountPath
            }
          ]
        }
      ]
      volumes: [
        {
          name: 'config'
          storageType: 'Secret'
          secrets: [
            {
              secretRef: 'sage-cloud-config'
              path: configFileName
            }
          ]
        }
      ]
    }
  }
  dependsOn: [
    sageAcrPull
  ]
}

// CAS BFF — external container ingress with its custom domain bound through the
// environment certificate. Runs as the BFF identity; its confidential client
// secret is a Key Vault reference, and its session store authenticates to
// Postgres by the same identity.
resource bffApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-cas-bff-${environmentName}'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${bffIdentityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: acaEnvironmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8001
        transport: 'auto'
        customDomains: [
          {
            name: casHostname
            certificateId: casCertificateId
            bindingType: 'SniEnabled'
          }
        ]
      }
      registries: [
        {
          server: acrLoginServer
          identity: bffIdentityId
        }
      ]
      secrets: [
        {
          name: 'bff-cloud-config'
          value: bffConfigYaml
        }
        {
          name: 'bff-client-secret'
          keyVaultUrl: bffClientSecretUri
          identity: bffIdentityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'cas-bff'
          image: '${acrLoginServer}/bff:${imageTag}'
          env: [
            {
              name: 'SAGE_CONFIG_PATH'
              value: '${bffConfigMountPath}/${configFileName}'
            }
            {
              name: 'AZURE_CLIENT_ID'
              value: bffIdentityClientId
            }
            {
              name: 'CAS_BFF_TENANT_ID'
              value: tenantId
            }
            {
              name: 'CAS_BFF_CLIENT_ID'
              value: bffOidcClientId
            }
            {
              name: 'CAS_BFF_CLIENT_SECRET'
              secretRef: 'bff-client-secret'
            }
            {
              name: 'CAS_BFF_SAGE_APP_ID_URI'
              value: sageAudience
            }
            {
              name: 'CAS_BFF_SAGE_BASE_URL'
              value: 'https://${sageHostname}'
            }
          ]
          volumeMounts: [
            {
              volumeName: 'config'
              mountPath: bffConfigMountPath
            }
          ]
        }
      ]
      volumes: [
        {
          name: 'config'
          storageType: 'Secret'
          secrets: [
            {
              secretRef: 'bff-cloud-config'
              path: configFileName
            }
          ]
        }
      ]
    }
  }
  dependsOn: [
    bffAcrPull
  ]
}

@description('Deterministic FQDN of the SAGE container app — the value the APIM facade backend resolves from.')
output sageFqdn string = sageApp.properties.configuration.ingress.fqdn

@description('Resource id of the SAGE container app.')
output sageContainerAppId string = sageApp.id

@description('Name of the SAGE container app — the deploy pipeline restarts it by name to converge the app tier after the bootstrap job runs.')
output sageContainerAppName string = sageApp.name

@description('Deterministic FQDN of the CAS BFF container app (its Route 53 records publish at deploy time).')
output bffFqdn string = bffApp.properties.configuration.ingress.fqdn

@description('Resource id of the CAS BFF container app.')
output bffContainerAppId string = bffApp.id

@description('Name of the CAS BFF container app — the deploy pipeline restarts it by name to converge the app tier after the bootstrap job runs.')
output bffContainerAppName string = bffApp.name
