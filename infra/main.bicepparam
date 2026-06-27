using './main.bicep'

// Single cloud environment. The deployment what-if/create commands pass
// --location to record the subscription-scope deployment; keep that value
// in step with the location below. Additional environments are added by
// copying this file to main.<env>.bicepparam and selecting it in the
// workflow — see docs/process/azure-deployment.md.

param environmentName = 'prod'
param location = 'eastus2'
param resourceGroupName = 'rg-cas-prod'

// APIM facade coordinates. The SAGE audience is a placeholder until the Entra
// registration is concrete; the operator substitutes the real value at deploy
// time. The facade backend hostname is no longer a parameter — it resolves from
// the SAGE container app's FQDN. Kept free of identity GUIDs so the deployment
// surface stays clean.
param sageAudience = 'api://REPLACE-WITH-SAGE-APP-ID'
param publisherEmail = 'ops@cas.invalid'
param apimSku = 'Consumption'

// Container-app deploy coordinates. The image tag is the immutable
// {version}-{short-sha} the deploy pins (never `latest`); the BFF OIDC client id
// is the confidential-client app registration id. Both are substituted at deploy
// time and kept free of identity GUIDs here.
param imageTag = 'REPLACE-WITH-IMAGE-TAG'
param bffOidcClientId = 'REPLACE-WITH-BFF-CLIENT-ID'

// Owned base domain the cas/sage custom hostnames derive from. The operator
// substitutes the real zone; the wildcard certificate *.<base-domain> (loaded
// in Key Vault as wildcard-tls) covers both hostnames. DNS is published
// manually by the operator — see docs/process/custom-domains-dns.md.
param baseDomain = 'REPLACE-WITH-OWNED-DOMAIN'

// Document-store vault-source binding coordinates (CAS-ADR-043). The SharePoint
// site and document-library drive ids that host the cloud vault tree; the
// operator resolves them after granting the SAGE managed identity the
// site-scoped Microsoft Graph permission — see
// docs/process/sharepoint-vault-source.md. Left as placeholders until then; the
// root path defaults to 'vaults'.
param sharepointSiteId = 'REPLACE-WITH-SHAREPOINT-SITE-ID'
param sharepointDriveId = 'REPLACE-WITH-SHAREPOINT-DRIVE-ID'
