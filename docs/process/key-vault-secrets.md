# Loading Key Vault secrets and the TLS certificate

The cloud deployment profile (CAS-ADR-042) keeps its secrets in an Azure Key
Vault. The Bicep Key Vault module provisions the vault and its RBAC access model
but, by design, commits **no secret material** — secret values and the owned
wildcard TLS certificate are loaded out of band by the one-time operator step
documented here. This mirrors the no-stored-credential posture of the deploy
identity (see `azure-deployment.md`) and the app registrations (see
`entra-app-registrations.md`).

**Codified as [`deploy/bootstrap/load-key-vault-secrets.sh`](../../deploy/bootstrap/load-key-vault-secrets.sh).** That script is the executable substance of this procedure (CAS Cloud Deployment Discipline, Principle 3); this runbook documents it.

What the vault holds:

- **`anthropic-api-key`** — the hosted abstraction provider's API key, read at
  runtime by the SAGE container app via its managed identity.
- **`bff-client-secret`** — the BFF OIDC client secret (the Entra app
  registration credential), read at runtime by the BFF container app via its
  managed identity.
- **`wildcard-tls`** — the owned wildcard TLS certificate, sourced by the
  custom-domain bindings.
- **`sage-ingress-key`** — the key the API Management gateway injects on every
  request it forwards to SAGE (as the `X-SAGE-Ingress-Key` header) and SAGE
  requires, read by both at runtime via their own managed identities. SAGE's
  container ingress is public (the Consumption gateway has no virtual network),
  so this key is what keeps callers from reaching SAGE except through the
  gateway. The loader generates it; nobody needs to know its value.

Each identity is granted **Key Vault Secrets User** on exactly the secrets it
reads, never on the vault, which may hold other workloads' secrets: SAGE reads
`anthropic-api-key` and `sage-ingress-key`; the BFF reads `bff-client-secret`
and `wildcard-tls` (for the container-apps environment certificate); the
gateway reads `wildcard-tls` and `sage-ingress-key`.

The database connection authenticates by **managed identity**, so there is no
database password secret to load.

The secret and certificate names above are fixed: they are the `anthropicSecretName`,
`bffClientSecretName`, `tlsCertificateName` and `ingressKeySecretName` outputs of the
Key Vault module, and downstream configuration builds Key Vault references from them.
Use them verbatim.

At runtime the SAGE container app finds the vault and identity through two
non-secret environment coordinates the container app injects: `SAGE_KEY_VAULT_URI`
(the vault's data-plane URI, the `keyVaultUri` deployment output) and
`AZURE_CLIENT_ID` (the SAGE managed identity's client id, which selects the
user-assigned identity the app authenticates with). Neither carries a secret
value.

## Prerequisites

- The infrastructure deployment has run and created the vault. Its name is the
  `keyVaultName` deployment output (RBAC-authorized, so access is by role
  assignment, not an access policy).
- You hold **Key Vault Secrets Officer** and **Key Vault Certificates Officer**
  on the vault. These are write roles, distinct from the read-only **Key Vault
  Secrets User** grants the module makes, per secret, to the SAGE, CAS BFF and
  gateway managed identities — the workloads read; only an operator writes.
- `az login` to the subscription that owns the resource group.

Resolve the vault name from the deployment outputs:

```bash
KV=$(az deployment sub show --name <deployment-name> \
  --query 'properties.outputs.keyVaultName.value' -o tsv)
```

## Keep secret values off the command line

A command-line argument is visible to every process on the machine for as long
as the command runs, so no secret value is passed as one. Each value goes into
a file readable only by you, in a scratch directory removed afterwards, and the
CLI reads it from there: `--file` for a secret, `@<file>` for the certificate
password, `file:<path>` for OpenSSL. `printf` is a shell builtin, so writing
the value does not expose it either. The loader script does exactly this:

```bash
umask 077
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
```

## Load the abstraction-provider API key

Read the value from a secure source — do not paste it into shell history. The
example reads it from an environment variable that is `unset` immediately after:

```bash
printf '%s' "$ANTHROPIC_API_KEY" >"$scratch/anthropic"
unset ANTHROPIC_API_KEY
az keyvault secret set --vault-name "$KV" --name anthropic-api-key \
  --file "$scratch/anthropic" --encoding utf-8 --output none
```

Setting the same secret again adds a new version (the rotation path); the apps
resolve the current version.

## Load the BFF OIDC client secret

Read the value from a secure source — do not paste it into shell history. The
example reads it from an environment variable that is `unset` immediately after:

```bash
printf '%s' "$BFF_CLIENT_SECRET" >"$scratch/bff"
unset BFF_CLIENT_SECRET
az keyvault secret set --vault-name "$KV" --name bff-client-secret \
  --file "$scratch/bff" --encoding utf-8 --output none
```

## Generate the gateway ingress key

Generated once, when the vault holds none; a re-run keeps the existing key, so
the gateway and SAGE keep agreeing on it. The key travels as a header value, so
it is taken through a variable, which drops the generator's trailing newline:

```bash
if [ -z "$(az keyvault secret list --vault-name "$KV" \
    --query "[?name=='sage-ingress-key'].id" -o tsv)" ]; then
  ingress_key="$(openssl rand -hex 32)"
  printf '%s' "$ingress_key" >"$scratch/ingress"
  unset ingress_key
  az keyvault secret set --vault-name "$KV" --name sage-ingress-key \
    --file "$scratch/ingress" --encoding utf-8 --output none
fi
```

**Rotating it** is a planned step, because the gateway and SAGE read it
independently. Set a new version as above, have API Management refresh the
`sage-ingress-key` named value, and restart the SAGE revision. Until both hold
the new value, SAGE refuses the gateway's requests.

## Import the wildcard TLS certificate

The bundle **must contain the full chain** — the leaf certificate **and** its
issuing intermediate(s). Azure Container Apps serves the bound environment
certificate's PFX bytes verbatim, so a leaf-only bundle makes the BFF custom
domain (`cas.<base-domain>`) fail strict TLS clients (`curl` error 60,
"certificate not trusted"), even though the SAGE host (`sage.<base-domain>`)
keeps working because API Management rebuilds the chain from its own store. A
leaf-only PFX is the most common cause of a trusted-on-my-machine,
untrusted-in-CI custom-domain endpoint.

Build a full-chain PFX from the leaf, its private key, and the issuer's
intermediate (the CA's download page, or the `CA Issuers` URL in the leaf's
Authority Information Access extension):

```bash
openssl pkcs12 -export -inkey key.pem \
  -in leaf.pem -certfile intermediate.pem \
  -out wildcard-fullchain.pfx
# confirm the bundle carries >= 2 certificates BEFORE importing:
printf '%s' "$WILDCARD_TLS_PFX_PASSWORD" >"$scratch/pfx-password"
openssl pkcs12 -nokeys -in wildcard-fullchain.pfx -passin "file:$scratch/pfx-password" \
  | grep -c 'BEGIN CERTIFICATE'   # -> 2 or more (leaf + intermediate)
```

Import the full-chain bundle:

```bash
az keyvault certificate import --vault-name "$KV" --name wildcard-tls \
  --file <path-to-fullchain.pfx> --password "@$scratch/pfx-password" --output none
```

`deploy/bootstrap/load-key-vault-secrets.sh` runs this import and **refuses a
leaf-only bundle** (the same `>= 2 certificates` guard) so the gap cannot reach
Key Vault unnoticed. Certificate renewal is the same command with a new
full-chain bundle; the custom-domain bindings pick up the current version.

After a deploy, confirm the served chain is complete and trusted:

```bash
echo | openssl s_client -connect cas.<base-domain>:443 \
  -servername cas.<base-domain> -showcerts 2>/dev/null \
  | grep -c 'BEGIN CERTIFICATE'   # -> 2 or more
echo | openssl s_client -connect cas.<base-domain>:443 \
  -servername cas.<base-domain> 2>/dev/null \
  | grep 'Verify return code'     # -> 0 (ok)
```

The post-deploy preflight's `bff_custom_domain_tls` check asserts exactly this.

## Verify

```bash
az keyvault secret show --vault-name "$KV" --name anthropic-api-key \
  --query 'attributes.enabled' -o tsv   # -> true
az keyvault secret show --vault-name "$KV" --name bff-client-secret \
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
