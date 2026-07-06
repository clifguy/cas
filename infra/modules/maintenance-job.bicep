// CAS cloud deployment — maintenance job module.
//
// Declares the Container Apps Job that runs the out-of-band cloud maintenance
// operations for the cloud deployment profile (CAS-ADR-043/042/029): whole-vault
// teardown and document purge. A cloud vault's durable state lives in places a
// developer laptop cannot reach: the Postgres schemas on the Entra-only,
// VNet-integrated Flexible Server, and the retained-source tree in a SharePoint
// document library reached over Microsoft Graph. So these operations run from one
// generalized job inside the same VNet-integrated Container Apps environment,
// invoking the committed, unit-tested `sage.maintenance.cloud_job` dispatcher,
// which routes on the per-invocation `SAGE_MAINTENANCE_COMMAND` override.
//
// The job runs as the SAGE workload identity — the one identity that already holds
// every grant the maintenance operations need: it owns the per-vault Postgres
// schemas it created at provision time (so it can DROP them and write their audit
// tables) and holds the SharePoint `Sites.Selected` write grant (so it can delete
// the vault folder). No new identity and no new Azure permission are introduced.
// The image is pulled by that identity, whose registry AcrPull grant the
// container-apps module already declares — this module does not re-declare it (a
// second role assignment with the same deterministic name would clash).
//
// Manual trigger: the job is declared by the deploy and started out-of-band by the
// dedicated maintenance workflow, which supplies the per-invocation request (the
// command selector, the target, typed confirmations, apply/snapshot flags) as
// `az containerapp job start --env-vars` overrides — never baked here. Like the
// sibling postgres-bootstrap job, this is provisioning-as-code: declared by the
// deploy, started on demand.

@description('Azure region for the maintenance job.')
param location string

@description('Short environment name, e.g. prod. Used in resource naming.')
param environmentName string

@description('Tags applied to the maintenance job.')
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

@description('Name of the database the maintenance operations run against.')
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

// The one-shot maintenance job. Manual trigger: declared by the deploy, started
// out-of-band by the maintenance workflow with the per-invocation request injected
// as env-var overrides. No auto-retry — a destructive operation re-runs only on an
// explicit operator dispatch (the entrypoints are idempotent, so a resumed run is
// safe, but re-execution stays deliberate).
resource maintenanceJob 'Microsoft.App/jobs@2024-03-01' = {
  name: 'job-maintenance-${environmentName}'
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
          name: 'maintenance'
          image: '${acrLoginServer}/sage:${imageTag}'
          command: [
            'python'
            '-m'
            'sage.maintenance.cloud_job'
          ]
          // Standing coordinates only. The per-invocation maintenance request —
          // the command selector plus its per-command request (the
          // SAGE_DELETE_* and SAGE_PURGE_* families) — is supplied by the
          // maintenance workflow as job-start env-var overrides, never baked
          // here — so the job as deployed cannot delete or purge anything.
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

@description('Name of the maintenance Container Apps Job (the maintenance workflow starts it out-of-band).')
output maintenanceJobName string = maintenanceJob.name
