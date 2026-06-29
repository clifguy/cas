# Custom domains, TLS, and the AWS Route 53 handoff

The CAS cloud deployment profile (CAS-ADR-042) serves two public hostnames, both
under one owned base domain and both secured by the owned **wildcard TLS
certificate**:

- **`cas.<BASE_DOMAIN>`** — the CAS BFF, fronted by the **container ingress** of
  its Azure Container App.
- **`sage.<BASE_DOMAIN>`** — the SAGE edge, fronted by the **API Management**
  facade (APIM is SAGE's public edge; the BFF does not go through it).

The wildcard certificate (`*.<BASE_DOMAIN>`) is loaded into Key Vault out of band
as `wildcard-tls` (see [`key-vault-secrets.md`](key-vault-secrets.md)) and bound
to Azure **by reference** — the Bicep deployment never carries certificate
material. Because the certificate is brought via Key Vault, there is **no DNS-01
ACME validation**: Azure does not provision the certificate, so the only DNS the
operator publishes is the records below.

The DNS zone lives in **AWS Route 53**, not Azure DNS. Record publication is
therefore a manual operator step, documented here, exactly as the Entra
registrations ([`entra-app-registrations.md`](entra-app-registrations.md)) and
the Key Vault secrets ([`key-vault-secrets.md`](key-vault-secrets.md)) are
operator bootstraps that live in the repo as procedures, not as Azure resources.

**Codified as [`deploy/bootstrap/emit-dns-records.sh`](../../deploy/bootstrap/emit-dns-records.sh).** That script computes and emits the records below (CAS Cloud Deployment Discipline, Principle 3); this runbook documents them. The script is provider-agnostic — it only prints records; publishing them in the zone is the manual step.

## Chosen approach: manual Route 53 records, certificate by Key Vault reference

The records are published by hand (or by the operator's own Route 53 tooling)
against the AWS-hosted zone. Azure DNS was not adopted: the zone of record is in
Route 53, and splitting authority across two clouds for a handful of records
would buy nothing. The certificate binding is a Key Vault reference on both edges
— the ACA environment imports it as an environment certificate, and APIM binds it
as a gateway hostname configuration — so certificate material stays in the vault
and rotation flows to both bindings without a redeploy.

## Prerequisites

- The infrastructure deployment has run. `cas.<BASE_DOMAIN>` and
  `sage.<BASE_DOMAIN>` are the `casCustomDomain` and `sageCustomDomain` deployment
  outputs; `<BASE_DOMAIN>` is the `baseDomain` deployment parameter.
- The `wildcard-tls` certificate is loaded in Key Vault
  ([`key-vault-secrets.md`](key-vault-secrets.md)). The managed identities that
  read it (SAGE for APIM, the CAS BFF for the ACA environment certificate) hold
  **Key Vault Certificate User** — granted by the Key Vault module — so the
  bindings can read the certificate.
- You hold write access to the `<BASE_DOMAIN>` hosted zone in AWS Route 53.
- `az login` to the subscription that owns the resource group, for resolving the
  Azure FQDNs and the domain-ownership token below.

Resolve the deployment outputs (never paste literals):

```bash
SAGE_HOST="$(az deployment sub show --name <deployment-name> \
  --query 'properties.outputs.sageCustomDomain.value' -o tsv)"   # sage.<BASE_DOMAIN>
CAS_HOST="$(az deployment sub show --name <deployment-name> \
  --query 'properties.outputs.casCustomDomain.value' -o tsv)"    # cas.<BASE_DOMAIN>
```

## The `sage` records (APIM edge — bindable as soon as APIM is deployed)

APIM serves `sage.<BASE_DOMAIN>` from its gateway. Point the hostname at APIM's
default gateway host with a CNAME. Resolve the gateway host from the output:

```bash
APIM_GATEWAY="$(az deployment sub show --name <deployment-name> \
  --query 'properties.outputs.apimGatewayUrl.value' -o tsv)"     # https://<APIM_NAME>.azure-api.net
APIM_HOST="${APIM_GATEWAY#https://}"                             # <APIM_NAME>.azure-api.net
```

Publish in Route 53:

| Record | Type | Value |
| --- | --- | --- |
| `sage.<BASE_DOMAIN>` | CNAME | `<APIM_NAME>.azure-api.net` (the APIM gateway host) |

APIM validates ownership by serving the bound certificate over the CNAME'd
hostname; the Key Vault certificate reference and the gateway hostname
configuration are already in place from the deployment, so the binding completes
once the CNAME resolves. No TXT record is required on the APIM side.

## The `cas` records (container ingress — resolved at the deploy step)

`cas.<BASE_DOMAIN>` binds to the **CAS BFF container app's** ingress. The
environment-level wildcard certificate is imported by the infrastructure
deployment, but the container-app ingress binding — and therefore the two values
the operator needs below — exist only once the BFF container app itself is
created, which is the **end-to-end deploy step**, not this binding step.

At the deploy step, resolve the container app's ingress FQDN and its
domain-ownership verification token:

```bash
BFF_FQDN="$(az containerapp show --name <bff-app-name> --resource-group <rg> \
  --query 'properties.configuration.ingress.fqdn' -o tsv)"
VERIFICATION_ID="$(az containerapp show --name <bff-app-name> --resource-group <rg> \
  --query 'properties.customDomainVerificationId' -o tsv)"
```

Then publish in Route 53:

| Record | Type | Value |
| --- | --- | --- |
| `cas.<BASE_DOMAIN>` | CNAME | `<BFF_APP_FQDN>` (the container app ingress FQDN) |
| `asuid.cas.<BASE_DOMAIN>` | TXT | `<VERIFICATION_ID>` (the container app domain-ownership token) |

The `asuid` TXT proves domain ownership to Azure Container Apps; the CNAME routes
traffic. The BFF container app then attaches the environment wildcard certificate
to the `cas.<BASE_DOMAIN>` custom domain through its ingress configuration.

Unlike APIM (which rebuilds the chain for the `sage` edge), Azure Container Apps
serves the environment certificate's PFX bytes **verbatim** — so the
`wildcard-tls` bundle in Key Vault **must carry the full chain** (leaf +
intermediate). A leaf-only bundle makes `cas.<BASE_DOMAIN>` fail strict TLS
clients while `sage.<BASE_DOMAIN>` keeps working. See
[`key-vault-secrets.md`](key-vault-secrets.md) for building a full-chain PFX; the
post-deploy preflight's `bff_custom_domain_tls` check enforces it.

## Cross-cloud ordering

The one sequencing subtlety is that Azure must emit a hostname (and, for the
`cas` side, an ownership token) before the operator can publish the AWS records,
and the binding only completes after the records resolve:

1. **Azure emits.** The Bicep deployment binds the certificate and exposes the
   Azure FQDNs (the APIM gateway host; and, at the deploy step, the BFF ingress
   FQDN and its domain-ownership validation token).
2. **The operator publishes.** Using the emitted values, the operator writes the
   Route 53 CNAME records (both hostnames) and the `asuid` ownership TXT (the
   `cas` side) into the AWS-hosted `<BASE_DOMAIN>` zone.
3. **The binding completes.** Once the records resolve, Azure serves each
   hostname over HTTPS with the wildcard certificate — APIM for `sage`, the
   container ingress for `cas`.

The `sage` records can be published as soon as the APIM facade is deployed; the
`cas` records wait for the BFF container app at the deploy step, because their
target FQDN and verification token do not exist until then.

## Verify

```bash
dig +short CNAME "$SAGE_HOST"          # -> <APIM_NAME>.azure-api.net
curl -sI "https://${SAGE_HOST}/" | head -n 1   # -> HTTP/1.1 200 (or the API's auth challenge)
```

For `cas`, after the deploy step publishes its records:

```bash
dig +short CNAME "$CAS_HOST"           # -> <BFF_APP_FQDN>
dig +short TXT "asuid.${CAS_HOST}"     # -> the verification id
curl -sI "https://${CAS_HOST}/" | head -n 1
```

A bound hostname serving the wildcard certificate over HTTPS is the success
condition for each edge.

## What this procedure does NOT do

- **Create the CAS BFF container app or its ingress custom-domain binding.** The
  environment wildcard certificate is imported by this binding step; attaching it
  to the `cas.<BASE_DOMAIN>` custom domain on the BFF container app, and
  publishing the `cas` Route 53 records, are the end-to-end deploy step.
- **Manage the certificate.** The wildcard certificate is loaded and rotated in
  Key Vault out of band ([`key-vault-secrets.md`](key-vault-secrets.md)); the
  bindings follow the current version because they reference it versionlessly.
- **Switch the MCP-advertised resource URL.** The APIM facade advertises its
  resource-metadata URL to MCP clients; pointing that at `sage.<BASE_DOMAIN>`
  once the custom domain resolves is part of the end-to-end deploy wiring, not
  this binding step.
