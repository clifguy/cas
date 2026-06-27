// CAS cloud deployment — Postgres bootstrap job module.
//
// Declares the Container Apps Job that runs the idempotent, admin-side database
// bootstrap for the cloud deployment profile (CAS-ADR-042). The managed Postgres
// server is Entra-only and integrated into a delegated subnet with no public
// endpoint, so the role and extension SQL — which is data-plane and cannot be
// expressed as declarative ARM — runs from a job inside the same VNet-integrated
// Container Apps environment. The job runs as the dedicated bootstrap identity
// (set as the server's Entra administrator by the relational-store module), pulls
// the SAGE image by that identity, and invokes the committed, unit-tested
// `sage.storage.postgres.cloud_bootstrap` entrypoint: it pre-creates the
// extensions and enrols each application managed identity as a least-privilege
// database role (CONNECT + CREATE on the database). Re-running converges.
//
// The bootstrap is provisioning-as-code (CAS Cloud Deployment Discipline,
// Principle 3): declared by the deploy, started on bring-up. Resource-group
// scoped (the Bicep default); the orchestrator deploys it with scope: rg,
// composing the foundation, identity, and relational-store module outputs.

@description('Azure region for the bootstrap job.')
param location string

@description('Short environment name, e.g. prod. Used in resource naming.')
param environmentName string

@description('Tags applied to the bootstrap job.')
param tags object

@description('Resource id of the Azure Container Apps environment the job runs in (VNet-integrated, so it can reach the private Postgres subnet).')
param acaEnvironmentId string

@description('Login server host of the Azure Container Registry the SAGE image is pulled from.')
param acrLoginServer string

@description('Name of the Azure Container Registry (referenced existing to scope the AcrPull grant).')
param acrName string

@description('Immutable SAGE image tag the job runs ({version}-{short-sha}); never `latest`.')
param imageTag string

@description('Resource id of the bootstrap managed identity the job runs as (the server Entra administrator).')
param bootstrapIdentityId string

@description('Client id of the bootstrap managed identity (selects it for DefaultAzureCredential in the job).')
param bootstrapIdentityClientId string

@description('Principal id of the bootstrap managed identity (granted AcrPull).')
param bootstrapIdentityPrincipalId string

@description('Name of the bootstrap managed identity (the database user the job connects as under Entra auth).')
param bootstrapIdentityName string

@description('Fully qualified domain name of the managed Postgres server.')
param postgresServerFqdn string

@description('Name of the database the application roles are granted on.')
param postgresDatabaseName string

@description('Resource id of the SAGE managed identity (its name becomes a database role).')
param sageIdentityId string

@description('Resource id of the CAS BFF managed identity (its name becomes a database role).')
param bffIdentityId string

// Built-in Azure role: AcrPull (data-plane image pull). A public, fixed constant
// — not an identity coordinate.
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d' // gitleaks:allow public role id

// The application identities become database roles named exactly by their
// managed-identity name (the Entra principal each access token represents),
// derived from the identity resource id — the same derivation the container-apps
// module applies for each app's libpq user.
var sageDbRole = last(split(sageIdentityId, '/'))
var bffDbRole = last(split(bffIdentityId, '/'))

// The registry, referenced existing so the AcrPull grant scopes to it without
// re-declaring (and clobbering) the foundation's registry.
resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: acrName
}

// AcrPull for the bootstrap identity, scoped to the registry — the job pulls the
// SAGE image with no stored credential. Mirrors the container-apps module's grant
// pattern (guid name, subscription-scoped role-definition id, ServicePrincipal).
resource bootstrapAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, bootstrapIdentityPrincipalId, acrPullRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: bootstrapIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// The one-shot bootstrap job. Manual trigger: declared by the deploy and started
// on bring-up (the operator doc records the trigger). It reuses the SAGE image —
// which already carries psycopg and azure-identity — and runs the codified
// bootstrap entrypoint as the bootstrap identity, whose Entra token authenticates
// as the server administrator.
resource bootstrapJob 'Microsoft.App/jobs@2024-03-01' = {
  name: 'job-pg-bootstrap-${environmentName}'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${bootstrapIdentityId}': {}
    }
  }
  properties: {
    environmentId: acaEnvironmentId
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 600
      replicaRetryLimit: 1
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [
        {
          server: acrLoginServer
          identity: bootstrapIdentityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'pg-bootstrap'
          image: '${acrLoginServer}/sage:${imageTag}'
          command: [
            'python'
            '-m'
            'sage.storage.postgres.cloud_bootstrap'
          ]
          env: [
            {
              name: 'AZURE_CLIENT_ID'
              value: bootstrapIdentityClientId
            }
            {
              name: 'PG_FQDN'
              value: postgresServerFqdn
            }
            {
              name: 'PG_DATABASE'
              value: postgresDatabaseName
            }
            {
              name: 'PG_ADMIN_USER'
              value: bootstrapIdentityName
            }
            {
              name: 'SAGE_DB_ROLE'
              value: sageDbRole
            }
            {
              name: 'BFF_DB_ROLE'
              value: bffDbRole
            }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
    }
  }
  dependsOn: [
    bootstrapAcrPull
  ]
}

@description('Name of the bootstrap Container Apps Job (the operator starts it on bring-up).')
output bootstrapJobName string = bootstrapJob.name
