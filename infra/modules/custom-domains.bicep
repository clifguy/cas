// CAS cloud deployment — custom-domain certificate module.
//
// Binds the owned wildcard TLS certificate to the Azure Container Apps
// environment for the cloud deployment profile (CAS-ADR-042). The certificate
// is imported into the environment by reference from Key Vault — never as
// committed PFX material — so the no-stored-secret posture of the deploy path
// holds. Once bound here, a container app in the environment (the CAS BFF)
// attaches the certificate to its custom domain through its ingress
// configuration at deploy time; that container-app binding and the AWS Route 53
// record publication are the deploy step (see docs/process/custom-domains-dns.md).
//
// The companion `sage` custom domain is bound on the API Management facade in
// apim.bicep (a service hostname configuration), since APIM — not the container
// ingress — is SAGE's public edge.
//
// Resource-group scoped (the Bicep default): the orchestrator deploys it with
// scope: rg, composing the ACA environment name, the certificate's Key Vault
// secret URL, and the BFF managed identity through module outputs.

@description('Azure region for the certificate resource.')
param location string

@description('Tags applied to the certificate resource.')
param tags object

@description('Name of the Azure Container Apps environment the certificate binds to (referenced as an existing resource, never re-created here).')
param acaEnvironmentName string

@description('Versionless Key Vault secret URL of the owned wildcard certificate. Versionless so the binding follows certificate rotation.')
param tlsCertSecretUri string

@description('Canonical name the certificate is created under in the environment (the Key Vault certificate name).')
param tlsCertificateName string

@description('Resource id of the managed identity that reads the certificate from Key Vault (holds Key Vault Certificate User on the vault).')
param bffIdentityId string

// The environment foundation created. Referenced as existing so this module
// adds a certificate to it rather than re-declaring (and clobbering) it.
resource acaEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: acaEnvironmentName
}

// The wildcard certificate, imported into the environment by Key Vault
// reference. No PFX value or password is set here — the certificate material
// lives only in Key Vault (loaded out of band per
// docs/process/key-vault-secrets.md), and the managed identity authenticates
// the read.
resource wildcardCertificate 'Microsoft.App/managedEnvironments/certificates@2025-01-01' = {
  parent: acaEnvironment
  name: tlsCertificateName
  location: location
  tags: tags
  properties: {
    certificateKeyVaultProperties: {
      identity: bffIdentityId
      keyVaultUrl: tlsCertSecretUri
    }
  }
}

@description('Resource id of the environment certificate, attached to the CAS BFF container app custom domain at deploy time.')
output casCertificateId string = wildcardCertificate.id
