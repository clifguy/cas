// CAS cloud deployment — vault-teardown job module.
//
// Declares the Container Apps Job that runs the out-of-band whole-vault teardown
// for the cloud deployment profile (CAS-ADR-043/042/034). A cloud vault's durable
// state lives in two places a developer laptop cannot reach: the Postgres schema on
// the Entra-only, VNet-integrated Flexible Server, and the retained-source tree in
// a SharePoint document library reached over Microsoft Graph. So the teardown runs
// from a job inside the same VNet-integrated Container Apps environment, invoking
// the committed, unit-tested `sage.maintenance.delete_vault_cloud` entrypoint.
//
// The job runs as the SAGE workload identity — the one identity that already holds
// both grants the teardown needs: it owns the per-vault Postgres schemas it created
// at provision time (so it can DROP them) and holds the SharePoint `Sites.Selected`
// write grant (so it can delete the vault folder). No new identity and no new Azure
// permission are introduced. The image is pulled by that identity, whose registry
// AcrPull grant the container-apps module already declares — this module does not
// re-declare it (a second role assignment with the same deterministic name would
// clash).
//
// Manual trigger: the job is declared by the deploy and started out-of-band by the
// dedicated teardown workflow, which supplies the per-invocation request (vault id,
// typed confirmation, apply/snapshot flags) as `az containerapp job start
// --env-vars` overrides — never baked here. Like the sibling postgres-bootstrap
// job, this is provisioning-as-code: declared by the deploy, started on demand.

@description('Azure region for the teardown job.')
param location string

@description('Short environment name, e.g. prod. Used in resource naming.')
param environmentName string

@description('Tags applied to the teardown job.')
param tags object

@description('Resource id of the Azure Container Apps environment the job runs in (VNet-integrated, so it can reach the private Postgres subnet and Microsoft Graph).')
param acaEnvironmentId string

@description('Login server host of the Azure Container Registry the SAGE image is pulled from.')
param acrLoginServer string

@description('Immutable SAGE image tag the job runs ({version}-{short-sha}); never `latest`.')
param imageTag string

@description('Resource id of the SAGE managed identity the job runs as (schema owner + SharePoint Sites.Selected writer). Its name is the Postgres role the job connects as.')
param sageIdentityId string

@description('Client id of the SAGE managed identity (selects it for DefaultAzureCredential in the job — Postgres token, Graph token).')
param sageIdentityClientId string

@description('Fully qualified domain name of the managed Postgres server.')
param postgresServerFqdn string

@description('Name of the database the vault schema is dropped from.')
param postgresDatabaseName string

@description('Microsoft Graph site id of the SharePoint site that hosts the cloud vault tree.')
param sharepointSiteId string

@description('Microsoft Graph drive id of the SharePoint document library that holds the cloud vault tree.')
param sharepointDriveId string

@description('Folder path within the SharePoint document library the vault tree is rooted at.')
param vaultSourceRootPath string

// The SAGE identity's name is the Postgres role the job connects as, derived from
// the identity resource id — the same derivation the container-apps and bootstrap
// modules apply for each app's libpq user.
var sageDbRole = last(split(sageIdentityId, '/'))

// The one-shot teardown job. Manual trigger: declared by the deploy, started
// out-of-band by the teardown workflow with the per-invocation request injected as
// env-var overrides. No auto-retry — a destructive operation re-runs only on an
// explicit operator dispatch (the entrypoint is idempotent, so a resumed run is
// safe, but re-execution stays deliberate).
resource teardownJob 'Microsoft.App/jobs@2024-03-01' = {
  name: 'job-vault-teardown-${environmentName}'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${sageIdentityId}': {}
    }
  }
  properties: {
    environmentId: acaEnvironmentId
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 600
      replicaRetryLimit: 0
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [
        {
          server: acrLoginServer
          identity: sageIdentityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'vault-teardown'
          image: '${acrLoginServer}/sage:${imageTag}'
          command: [
            'python'
            '-m'
            'sage.maintenance.delete_vault_cloud'
          ]
          // Standing coordinates only. The per-invocation teardown request
          // (SAGE_DELETE_VAULT_ID / _CONFIRM / _APPLY / _SNAPSHOT / _REASON) is
          // supplied by the teardown workflow as job-start env-var overrides,
          // never baked here — so the job as deployed cannot delete anything.
          env: [
            {
              name: 'AZURE_CLIENT_ID'
              value: sageIdentityClientId
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
              name: 'PG_USER'
              value: sageDbRole
            }
            {
              name: 'SHAREPOINT_SITE_ID'
              value: sharepointSiteId
            }
            {
              name: 'SHAREPOINT_DRIVE_ID'
              value: sharepointDriveId
            }
            {
              name: 'SHAREPOINT_ROOT_PATH'
              value: vaultSourceRootPath
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
}

@description('Name of the vault-teardown Container Apps Job (the teardown workflow starts it out-of-band).')
output vaultTeardownJobName string = teardownJob.name
