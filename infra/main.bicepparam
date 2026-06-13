using './main.bicep'

// Single cloud environment. The deployment what-if/create commands pass
// --location to record the subscription-scope deployment; keep that value
// in step with the location below. Additional environments are added by
// copying this file to main.<env>.bicepparam and selecting it in the
// workflow — see docs/process/azure-deployment.md.

param environmentName = 'prod'
param location = 'eastus2'
param resourceGroupName = 'rg-cas-prod'

// APIM facade coordinates. The backend hostname and the SAGE audience are
// placeholders until the SAGE container app and the Entra registration are
// concrete (resolved at deploy time); the operator substitutes the real values
// then. Kept free of identity GUIDs so the deployment surface stays clean.
param sageBackendHostname = 'sage.REPLACE-WITH-ACA-DEFAULT-DOMAIN'
param sageAudience = 'api://REPLACE-WITH-SAGE-APP-ID'
param publisherEmail = 'ops@cas.invalid'
param apimSku = 'Consumption'

// Owned base domain the cas/sage custom hostnames derive from. The operator
// substitutes the real zone; the wildcard certificate *.<base-domain> (loaded
// in Key Vault as wildcard-tls) covers both hostnames. DNS lives in AWS Route
// 53 and is published manually — see docs/process/custom-domains-dns.md.
param baseDomain = 'REPLACE-WITH-OWNED-DOMAIN'
