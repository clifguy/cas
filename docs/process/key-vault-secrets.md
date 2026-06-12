# Loading Key Vault secrets and the TLS certificate

The cloud deployment profile (CAS-ADR-042) keeps its secrets in an Azure Key
Vault. The Bicep Key Vault module provisions the vault and its RBAC access model
but, by design, commits **no secret material** — secret values and the owned
wildcard TLS certificate are loaded out of band by the one-time operator step
documented here. This mirrors the no-stored-credential posture of the deploy
identity (see `azure-deployment.md`) and the app registrations (see
`entra-app-registrations.md`).

What the vault holds:

- **`anthropic-api-key`** — the hosted abstraction provider's API key, read at
  runtime by the SAGE container app via its managed identity.
- **`wildcard-tls`** — the owned wildcard TLS certificate, sourced by the
  custom-domain bindings.

The database connection authenticates by **managed identity**, so there is no
database password secret to load.

The secret and certificate names above are fixed: they are the `anthropicSecretName`
and `tlsCertificateName` outputs of the Key Vault module, and downstream
configuration builds Key Vault references from them. Use them verbatim.

## Prerequisites

- The infrastructure deployment has run and created the vault. Its name is the
  `keyVaultName` deployment output (RBAC-authorized, so access is by role
  assignment, not an access policy).
- You hold **Key Vault Secrets Officer** and **Key Vault Certificates Officer**
  on the vault. These are write roles, distinct from the read-only **Key Vault
  Secrets User** / **Key Vault Certificate User** roles the module grants the SAGE
  and CAS BFF managed identities — the apps read; only an operator writes.
- `az login` to the subscription that owns the resource group.

Resolve the vault name from the deployment outputs:

```bash
KV=$(az deployment sub show --name <deployment-name> \
  --query 'properties.outputs.keyVaultName.value' -o tsv)
```

## Load the abstraction-provider API key

Read the value from a secure source — do not paste it into shell history. The
example reads it from an environment variable that is `unset` immediately after:

```bash
az keyvault secret set --vault-name "$KV" --name anthropic-api-key \
  --value "$ANTHROPIC_API_KEY"
unset ANTHROPIC_API_KEY
```

Setting the same secret again adds a new version (the rotation path); the apps
resolve the current version.

## Import the wildcard TLS certificate

Import the owned wildcard certificate from a PFX/PKCS#12 bundle:

```bash
az keyvault certificate import --vault-name "$KV" --name wildcard-tls \
  --file <path-to-cert.pfx> --password <pfx-password>
```

Certificate renewal is the same command with the new bundle; the custom-domain
bindings pick up the current version.

## Verify

```bash
az keyvault secret show --vault-name "$KV" --name anthropic-api-key \
  --query 'attributes.enabled' -o tsv   # -> true
az keyvault certificate show --vault-name "$KV" --name wildcard-tls \
  --query 'id' -o tsv                   # -> the certificate id
```

The container apps fail closed with a clear error if a required secret is missing
or unreadable, so a deployment against an unseeded vault surfaces immediately
rather than silently degrading.

## What never goes in the repository

Secret values and certificate material are loaded only by the commands above and
live only in the vault. Nothing in `infra/`, the container image, or the
environment carries a secret value; the local profile's `.env` file is the
separate, local-only path and is git-ignored.
