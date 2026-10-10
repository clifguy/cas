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
- The email addresses of whoever owns the wildcard certificate, for
  `CERT_EXPIRY_CONTACTS`. See
  [Email the certificate's owner before expiry](#email-the-certificates-owner-before-expiry).

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
independently, and the loader keeps an existing key by design. Set a new
version directly, then have API Management refresh its named value and restart
the SAGE revision; until both hold the new value, SAGE refuses the gateway's
requests:

```bash
ingress_key="$(openssl rand -hex 32)"
printf '%s' "$ingress_key" >"$scratch/ingress"
unset ingress_key
az keyvault secret set --vault-name "$KV" --name sage-ingress-key \
  --file "$scratch/ingress" --encoding utf-8 --output none
APIM="$(az apim list -g "$RG" --query '[0].name' -o tsv)"
az rest --method POST --url "https://management.azure.com$(az apim show -g "$RG" -n "$APIM" \
  --query id -o tsv)/namedValues/sage-ingress-key/refreshSecret?api-version=2022-08-01"
az containerapp revision restart -g "$RG" -n "ca-sage-$ENV" \
  --revision "$(az containerapp show -g "$RG" -n "ca-sage-$ENV" \
    --query properties.latestRevisionName -o tsv)"
```

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
Key Vault unnoticed. Renewal reuses this import; see
[Renewing the wildcard certificate](#renewing-the-wildcard-certificate).

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

## Email the certificate's owner before expiry

The wildcard certificate is administered outside this deployment. Its issuer
warns the owner before it expires, but that warning does not say this
deployment still serves the certificate on both custom hostnames. An owner who
believes the certificate is retired can let it lapse, and both hostnames go
down together.

The vault closes that gap by emailing the owner itself. The certificate's
lifetime action emails the vault's certificate contacts a set number of days
before expiry, and the notice names the vault, so it identifies this deployment
as a holder of the certificate.

**Codified as [`deploy/bootstrap/set-certificate-expiry-notice.sh`](../../deploy/bootstrap/set-certificate-expiry-notice.sh).**
The loader runs it after every import, so the first load and each renewal set
it. Run it on its own to set or change the notice on an existing deployment
without re-importing:

```bash
KEY_VAULT_NAME="$KV" \
CERT_EXPIRY_CONTACTS="cert-owner@example.org,ops@example.org" \
  deploy/bootstrap/set-certificate-expiry-notice.sh
```

- **`CERT_EXPIRY_CONTACTS`** (required): the owner's addresses, comma-separated.
  The loader requires it too, and refuses a malformed entry before writing
  anything.
- **`CERT_EXPIRY_NOTICE_DAYS`** (optional, default 30): days before expiry to
  send the notice. Keep it above the renewal lead time described in
  [Renewing the wildcard certificate](#renewing-the-wildcard-certificate).

Certificate contacts belong to the vault, not to one certificate, and the vault
may hold other workloads' certificates. The script therefore only adds
contacts. It never removes one, and an address the vault already lists
(compared without regard to case) is left as it is. To drop a contact, use
`az keyvault certificate contact delete --vault-name "$KV" --email <address>`.
The lifetime action replaces the certificate's previous one. Re-running the
script changes nothing further.

The script reads the action back and fails if the vault does not report it.
To check it by hand:

```bash
az keyvault certificate contact list --vault-name "$KV" -o table
az keyvault certificate show --vault-name "$KV" --name wildcard-tls \
  --query 'policy.lifetimeActions' -o json
  # -> an EmailContacts action with trigger.daysBeforeExpiry set
```

Setting contacts and certificate policy needs **Key Vault Certificates
Officer**, which only the operator holds. The CI deploy identity cannot do it,
so this stays an operator step. The role grants certificate and contact
writes. Adding a contact also reads the existing list, so if that read is
refused the script stops before it changes the policy. On a deployment's first
run, keep the two readbacks above as the record that the notice is in place.

## Renewing the wildcard certificate

Two signals say renewal is due. Neither depends on the issuer's own reminder,
which does not name this deployment:

- **The vault's expiry notice.** The vault emails `CERT_EXPIRY_CONTACTS`
  `CERT_EXPIRY_NOTICE_DAYS` days before expiry (default 30), naming the vault
  that holds the certificate. This is the primary signal, because it fires
  whether or not anyone deploys. See
  [Email the certificate's owner before expiry](#email-the-certificates-owner-before-expiry).
- **The preflight's `wildcard_tls_expiry` check.** On every deploy, the cloud
  preflight reads the expiry that `sage.<base-domain>` and `cas.<base-domain>`
  each serve. It reports both dates, and fails when either falls within
  `PREFLIGHT_TLS_EXPIRY_WINDOW_DAYS` (default 30) or has passed. It is a
  backstop: it runs only when something is deployed. Inside the window it
  fails every deploy's post-deploy gate, and the steps after that gate do not
  run. To deploy while a renewal is pending, lower
  `PREFLIGHT_TLS_EXPIRY_WINDOW_DAYS` on the tenant's GitHub Environment; `0`
  fails only a certificate that has expired or expires within the day.

Import the renewed certificate as a full-chain PFX under the **same name**,
`wildcard-tls`, using the import command above. That command, and the loader
script with its `>= 2 certificates` guard, add a new version of the existing
certificate. Do not create a certificate under a new name, because downstream
configuration builds its references from that name.

No redeploy is needed. API Management and the Container Apps environment both
reference the certificate's secret without a version, so each one fetches the
current version on its own schedule:

- **API Management** picks up a new version through its automated
  synchronization job. Microsoft documents that job as taking "several hours or
  longer", and quotes up to one to two days for an auto-renewed certificate. To
  apply the new version at once, select **Sync certificates** on the instance's
  **Custom domains** page.
  ([Configure a custom domain name](https://learn.microsoft.com/azure/api-management/configure-custom-domain).)
- **Container Apps** applies a rotated Key Vault certificate within up to 12
  hours.
  ([Import certificates from Azure Key Vault](https://learn.microsoft.com/azure/container-apps/key-vault-certificates-manage).)

Both services keep serving their cached certificate until they fetch the new
version. Renew well ahead of expiry, by more than the longer of the two windows.
Acting on the 30-day notice leaves ample margin.

Once both windows have passed, or after a manual sync, verify with the cloud
preflight. `kv_wildcard_tls` checks that the current version still covers
`*.<base-domain>`. `bff_custom_domain_tls` checks that `cas.<base-domain>`
serves a complete, trusted chain. `wildcard_tls_expiry` reports the expiry each
edge serves: both should show the renewed certificate's date. To read the dates
by hand:

```bash
echo | openssl s_client -connect sage.<base-domain>:443 \
  -servername sage.<base-domain> 2>/dev/null | openssl x509 -noout -enddate
echo | openssl s_client -connect cas.<base-domain>:443 \
  -servername cas.<base-domain> 2>/dev/null | openssl x509 -noout -enddate
```

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
