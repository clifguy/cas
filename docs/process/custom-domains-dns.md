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
  read it (the API Management gateway's own identity, and the CAS BFF for the
  ACA environment certificate) hold **Key Vault Secrets User** on the
  certificate's backing secret alone — granted by the Key Vault module — so the
  bindings can read the certificate and nothing else in the vault.
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

## Changing the base domain

Moving a deployment from `<OLD_DOMAIN>` to `<NEW_DOMAIN>` changes every
hostname the edges serve and every identity derived from them. The
infrastructure and the workflows derive all of it from `BASE_DOMAIN`, so the
change in Azure is one configuration value. The order of the steps around that
value is what matters: each one must be in place before the step that relies on
it, and nothing belonging to the old domain is removed until clients have moved.

1. **Load the new certificate.** Import the `*.<NEW_DOMAIN>` wildcard as a
   full-chain PFX into `wildcard-tls`, under the same certificate name (see
   [`key-vault-secrets.md`](key-vault-secrets.md#import-the-wildcard-tls-certificate)).
   Both edges reference the certificate without a version, so each one moves to
   the new certificate when it next fetches the current version. From then on,
   the old hostnames no longer match the certificate they serve. Do this step
   immediately before the deploy, not days ahead of it. If the new certificate
   also covers `*.<OLD_DOMAIN>`, the old hostnames keep a matching certificate
   in the meantime. The loader sets the expiry notice again after the import;
   if the new certificate has a different owner, give their addresses in
   `CERT_EXPIRY_CONTACTS` (see
   [`key-vault-secrets.md`](key-vault-secrets.md#email-the-certificates-owner-before-expiry)).
2. **Add the new identities to Entra, additively.** On the SAGE resource-server
   registration, add the three `https://sage.<NEW_DOMAIN>` identifier URIs (the
   host and its `/mcp` and `/mcp_maint` forms) alongside the existing ones. On
   the CAS BFF registration, add the `https://cas.<NEW_DOMAIN>` callback redirect
   URI. Both collections are written by a full-set replace. Read the live set,
   write back the union, and read it back. Never write a set that holds only the
   new domain's entries, because that removes the old ones while clients still
   use them. See [`entra-app-registrations.md`](entra-app-registrations.md#1-sage-resource-server-registration)
   for the identifier URIs and
   [Redirect URIs on a re-run](entra-app-registrations.md#redirect-uris-on-a-re-run)
   for the redirect URI. The https identifier URIs need `<NEW_DOMAIN>` verified
   in the tenant first.
3. **Publish the new zone's records.** In the `<NEW_DOMAIN>` zone, publish the
   `sage` and `cas` CNAMEs and the `asuid.cas` TXT described above. Do this
   before the deploy that binds the `cas` custom domain, because Container Apps
   checks the ownership token when it binds. The BFF container app's ingress
   FQDN and verification token do not change with the domain, so they can be
   read from the running app ahead of the deploy.
4. **Set `BASE_DOMAIN` and deploy.** Set the `BASE_DOMAIN` variable on the
   tenant's GitHub Environment to `<NEW_DOMAIN>`. Then run the deploy. The
   `infra` workflow applies the infrastructure template, which rebinds both
   custom domains and the `sage-resource-url` named value. It then converges the
   application tier.
5. **Run the cloud preflight.** It runs as the deploy's post-deploy gate. When
   you re-run it by hand, set `BASE_DOMAIN` to `<NEW_DOMAIN>`. Also set
   `PREFLIGHT_RESOURCE_TOKEN_PROBE_CMD=deploy/resource-token-probe.sh`, from an
   authenticated `az` session, as the `infra` workflow does. Without it the
   registration check skips instead of verifying. These checks bear on the
   move:
   - `kv_wildcard_tls` checks that the certificate covers `*.<NEW_DOMAIN>`.
   - `bff_custom_domain_tls` checks the chain served on `cas.<NEW_DOMAIN>`.
   - `wildcard_tls_expiry` reports the expiry each new hostname serves, and
     fails if either is within the window.
   - `edge_resource_identity` and `edge_advertised_resources_registered` check
     that the edge advertises `https://sage.<NEW_DOMAIN>`, and that Entra holds
     that resource.
6. **Re-point MCP clients, per client type.** Move every client that names the
   SAGE edge to `https://sage.<NEW_DOMAIN>/mcp` (and `/mcp_maint`) and sign in
   again against the new resource. How depends on the client; see
   [Re-pointing MCP clients](#re-pointing-mcp-clients) below.
7. **Only then retire the old domain.** Remove the `https://sage.<OLD_DOMAIN>`
   identifier URIs and the old BFF redirect URI the same way: read the set, drop
   the entries, write back the remainder, and read it back. On the MCP client
   app, also remove any loopback callback that no configured client uses any
   more, such as the path a Codex server entry used before it was re-pointed.
   Then delete the `<OLD_DOMAIN>` records from its zone.

**Why the old hostname cannot stay as a serving alias for SAGE.** The SAGE edge
advertises exactly one resource origin. API Management's discovery operations
publish the `sage-resource-url` named value, which is `https://` plus the SAGE
custom domain, as the `resource` and authorization server in the
protected-resource metadata. An MCP client requires that advertised resource to
match the origin it connected to (RFC 9728). A client still on
`sage.<OLD_DOMAIN>` would therefore be handed `sage.<NEW_DOMAIN>` metadata and
reject it, even if the old hostname were still bound. Moving the clients (step 6)
is the only migration path. There is no window in which both names serve MCP.

### Re-pointing MCP clients

- **claude.ai custom connectors.** A connector's URL cannot be edited. Remove
  the connector (Customize → Connectors, then the connector's **⋮** menu →
  **Remove**). Add a new custom connector at `https://sage.<NEW_DOMAIN>/mcp`,
  and another at `/mcp_maint` if it is used, then sign in. The edge supports
  dynamic client registration, so no client ID is needed.
- **Codex.** After a server's `url` changes, Codex's next sign-in uses a new
  loopback callback path, `http://127.0.0.1:<callback_port>/callback/<id>`. The
  port stays as configured, but a pinned `callback_url` path is not reused.
  Entra rejects that sign-in with `AADSTS50011` until that exact URI is
  registered on the MCP client app (`cas-mcp-client`) as a public-client
  redirect URI. To register it:
  1. Read the current `publicClient.redirectUris`.
  2. Append the new URI.
  3. Write back the merged list with
     `az ad app update --public-client-redirect-uris <every URI in the merged list>`.
  4. Read it back.

  Never write a set without the existing entries: the flag replaces the whole
  collection, and the hosted client's callback would be lost. Then set that
  server's `callback_url` in the Codex configuration to the same URI. Entra
  ignores the port of a loopback redirect URI but matches its path exactly, so
  each Codex server entry needs its own registered path. Once registered, the
  path is stable across sign-ins. See
  [the MCP client registration](entra-app-registrations.md#4-public-mcp-client-registration-auth-code--pkce-no-secret)
  for how these entries sit alongside the bootstrap's own.
- **Other local registrations,** such as Claude Code's MCP entries: change the
  configured URL and sign in again.

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
