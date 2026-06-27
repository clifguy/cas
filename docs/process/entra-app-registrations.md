# Entra app registrations

The CAS cloud deployment profile (CAS-ADR-042) authenticates every surface with
tokens minted by **one Microsoft Entra issuer**. Two app registrations realize
that model:

- **SAGE** is an **OAuth resource server** — it exposes the scopes and app roles
  that authorize calls to its REST and MCP surfaces.
- **The CAS BFF** (the app backend, a backend-for-frontend) is a **confidential
  client** — it signs users in interactively and calls SAGE **on-behalf-of** the
  signed-in user, never as a service principal.

This runbook is the procedure that creates those two registrations. The concrete
auth binding for the `cloud` profile is enumerated in the *SAGE Deployment
Profile Bindings* steering document (the binding roster CAS-ADR-042 points to);
this file is the operational how-to, not the binding record.

Auth is **profile-gated**: the `local` profile runs with no auth at all, so these
registrations matter only to the `cloud` profile.

> Like the deploy identity in [`azure-deployment.md`](azure-deployment.md), the
> registrations are Microsoft Entra directory objects, not Azure resources. They
> are created **once, by hand**, in your tenant — the steps below are idempotent
> so a re-run reconciles rather than duplicates. They physically exist only after
> this procedure is run; the repo artifact is the procedure itself.

**Codified as [`deploy/bootstrap/entra-app-registrations.sh`](../../deploy/bootstrap/entra-app-registrations.sh).** That script is the executable substance of this procedure (CAS Cloud Deployment Discipline, Principle 3); this runbook documents it.

## Chosen approach: scripted `az`/Microsoft Graph, not Bicep

These registrations are provisioned by the scripted **`az ad`/`az rest`
Microsoft Graph** procedure below — a one-time bootstrap, run by an operator with
directory-admin rights, exactly as the CI deploy identity is bootstrapped.

The **Microsoft Graph Bicep extension** (declarative `Microsoft.Graph/applications`
resources) was considered and **not adopted**. App registrations are a one-time,
long-lived bootstrap rather than per-deployment resources, and the declarative
route would require granting the CI deploy service principal Microsoft Graph
`Application.ReadWrite.All` — a standing, tenant-wide write capability on every
app registration in the directory. Keeping the registrations out of the Bicep
deployment path keeps that privilege off the pipeline identity. The `az` CLI is
generally available; the Graph Bicep extension is still maturing.

## Prerequisites

Resolve identity coordinates at run time — never paste literals:

```bash
TENANT_ID="$(az account show --query tenantId -o tsv)"
REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)"   # <OWNER>/<REPO>
```

You will also choose two placeholders that downstream work fixes concretely:

- `<BFF_HOSTNAME>` — the cloud hostname the BFF is reachable at. It is fixed when
  the container ingress and the custom domain are provisioned; until then use the
  default ingress FQDN.
- `<AUTH_CALLBACK_PATH>` — the BFF's OIDC redirect path (for example
  `/auth/callback`); it is fixed by the BFF login implementation.

## 1. SAGE resource-server registration

Create (or reuse) the application, give it an application ID URI of the form
`api://<SAGE_APP_ID>`, and create its service principal. The lookup-then-create
guard makes the step idempotent.

```bash
SAGE_APP_ID="$(az ad app list --display-name sage-resource-server \
  --query '[0].appId' -o tsv)"
if [ -z "$SAGE_APP_ID" ]; then
  SAGE_APP_ID="$(az ad app create --display-name sage-resource-server \
    --sign-in-audience AzureADMyOrg --query appId -o tsv)"
fi

az ad app update --id "$SAGE_APP_ID" --identifier-uris "api://${SAGE_APP_ID}"
az ad sp create --id "$SAGE_APP_ID" 2>/dev/null || true
```

Expose the delegated scope and the app role. Both `oauth2PermissionScopes` and
`appRoles` are set in one Graph `PATCH`; generate the ids once and keep them so
re-runs are stable.

```bash
SAGE_OBJECT_ID="$(az ad app show --id "$SAGE_APP_ID" --query id -o tsv)"
ACCESS_SCOPE_ID="${ACCESS_SCOPE_ID:-$(uuidgen)}"
SAGE_READER_ROLE_ID="${SAGE_READER_ROLE_ID:-$(uuidgen)}"

az rest --method PATCH \
  --url "https://graph.microsoft.com/v1.0/applications/${SAGE_OBJECT_ID}" \
  --headers 'Content-Type=application/json' \
  --body "{
    \"api\": {
      \"oauth2PermissionScopes\": [{
        \"id\": \"${ACCESS_SCOPE_ID}\",
        \"value\": \"Sage.Access\",
        \"type\": \"User\",
        \"adminConsentDisplayName\": \"Access SAGE\",
        \"adminConsentDescription\": \"Access SAGE on behalf of the signed-in user.\",
        \"isEnabled\": true
      }]
    },
    \"appRoles\": [{
      \"id\": \"${SAGE_READER_ROLE_ID}\",
      \"allowedMemberTypes\": [\"User\"],
      \"value\": \"Sage.Reader\",
      \"displayName\": \"Sage.Reader\",
      \"description\": \"Read across the SAGE REST and MCP surfaces.\",
      \"isEnabled\": true
    }]
  }"
```

The single `Sage.Access` delegated scope (and the `Sage.Reader` app role) is the
authorization unit for **both** the REST surface and the MCP surface — SAGE does
not mint a separate scope per surface. Surface-specific authorization is enforced
in SAGE's token-validation layer, not by separate registrations here.

## 2. CAS BFF confidential-client registration

Create (or reuse) the BFF application as a confidential client, with its redirect
URI templated to the cloud hostname.

```bash
BFF_APP_ID="$(az ad app list --display-name cas-bff --query '[0].appId' -o tsv)"
if [ -z "$BFF_APP_ID" ]; then
  BFF_APP_ID="$(az ad app create --display-name cas-bff \
    --sign-in-audience AzureADMyOrg \
    --web-redirect-uris "https://<BFF_HOSTNAME>/<AUTH_CALLBACK_PATH>" \
    --query appId -o tsv)"
else
  az ad app update --id "$BFF_APP_ID" \
    --web-redirect-uris "https://<BFF_HOSTNAME>/<AUTH_CALLBACK_PATH>"
fi
az ad sp create --id "$BFF_APP_ID" 2>/dev/null || true
```

Grant the BFF the delegated API permission onto the SAGE resource server, then
admin-consent it. This `requiredResourceAccess` entry is what makes the
on-behalf-of exchange possible: the BFF, holding the user's token plus this
delegated grant, exchanges it for a SAGE-audienced token.

```bash
az ad app permission add --id "$BFF_APP_ID" \
  --api "$SAGE_APP_ID" \
  --api-permissions "${ACCESS_SCOPE_ID}=Scope"
az ad app permission admin-consent --id "$BFF_APP_ID"
```

On-behalf-of (OBO) requires the BFF to authenticate as a confidential client,
which means a client secret or certificate. Custody of that credential (Key Vault
plus a managed identity) is handled by the secrets capability of the cloud
profile, not by this registration step — do **not** store a secret in the repo.

## Uniform authorization across surfaces

SAGE is a **single** OAuth resource server with one application ID URI
(`api://<SAGE_APP_ID>`) and **one issuer** for the tenant. Every surface — REST
and MCP alike — validates the **same** Entra-issued token against that one
audience and that one issuer, so authorization is **uniform** across surfaces.
There is a **single issuer**: a token minted for the resource server is honored
identically on the REST and MCP surfaces, and the delegated user identity carried
by the BFF's on-behalf-of token reaches SAGE on every path.

## What this procedure does NOT do

These are deliberately separate changes, named here as capabilities:

- **Validating the token inside SAGE.** The resource-server enforcement that
  honors the Entra JWT uniformly across REST and MCP is SAGE-side code.
- **The BFF login flow.** Interactive OIDC sign-in, the on-behalf-of exchange,
  proxy-header handling, and externalized session state are BFF-side code.
- **The APIM edge.** The facade's JWT policy and the MCP discovery handshake are
  provisioned with the API-management module.
- **The binding record.** Adding the auth line to the `cloud` profile's roster in
  the *SAGE Deployment Profile Bindings* steering document is done with the
  profile-definition change.
- **Secret custody.** The BFF's confidential-client secret or certificate is
  loaded from Key Vault via a managed identity by the secrets capability.
- **The concrete hostname.** `<BFF_HOSTNAME>` is fixed when the container ingress
  and the custom domain are provisioned.
