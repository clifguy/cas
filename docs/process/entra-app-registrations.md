# Entra app registrations

The CAS cloud deployment profile (CAS-ADR-042) authenticates every surface with
tokens minted by **one Microsoft Entra issuer**. Three app registrations realize
that model:

- **SAGE** is an **OAuth resource server** — it exposes the scopes and app roles
  that authorize calls to its REST and MCP surfaces.
- **The CAS BFF** (the app backend, a backend-for-frontend) is a **confidential
  client** — it signs users in interactively and calls SAGE **on-behalf-of** the
  signed-in user, never as a service principal.
- **The public MCP client** is a **public client** (authorization-code + PKCE, no
  secret) — a standards-default MCP client authenticates directly against Entra
  through the DCR-compatibility facade at the SAGE edge (CAS-ADR-042), since
  Entra offers no real Dynamic Client Registration (RFC 7591).

Both interactive clients — the BFF and the public MCP client — gate sign-in on
membership in a single provisioning group (CAS-ADR-044).

This runbook is the procedure that creates those registrations and the sign-in
gate. The concrete auth binding for the `cloud` profile is enumerated in the
*SAGE Deployment Profile Bindings* steering document (the binding roster
CAS-ADR-042 points to); this file is the operational how-to, not the binding
record.

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

You will also choose placeholders that downstream work fixes concretely:

- `<BFF_HOSTNAME>` — the cloud hostname the BFF is reachable at. It is fixed when
  the container ingress and the custom domain are provisioned; until then use the
  default ingress FQDN.
- `<AUTH_CALLBACK_PATH>` — the BFF's OIDC redirect path (for example
  `/auth/callback`); it is fixed by the BFF login implementation.
- `<MCP_CLIENT_REDIRECT_URI>` — the public MCP client's registered redirect URI
  (a loopback or custom-scheme callback, not a CAS-controlled hostname); resolved
  against the chosen default MCP client's current documentation, not fixed here.
- `<SAGE_PUBLIC_HOSTNAME>` — the public SAGE hostname (the sage custom domain,
  `sage.<base-domain>`), registered as an https identifier URI on the resource
  server. Its host must sit under a domain verified in the tenant.

## 1. SAGE resource-server registration

Create (or reuse) the application, give it **four** identifier URIs — the
`api://<SAGE_APP_ID>` audience URI, the `https://<SAGE_PUBLIC_HOSTNAME>`
custom-domain identity, and the two MCP-mount forms of that identity — and
create its service principal. The lookup-then-create guard makes the step
idempotent.

```bash
SAGE_APP_ID="$(az ad app list --display-name sage-resource-server \
  --query '[0].appId' -o tsv)"
if [ -z "$SAGE_APP_ID" ]; then
  SAGE_APP_ID="$(az ad app create --display-name sage-resource-server \
    --sign-in-audience AzureADMyOrg --query appId -o tsv)"
fi

az ad app update --id "$SAGE_APP_ID" \
  --identifier-uris "api://${SAGE_APP_ID}" "https://${SAGE_PUBLIC_HOSTNAME}" \
    "https://${SAGE_PUBLIC_HOSTNAME}/mcp" "https://${SAGE_PUBLIC_HOSTNAME}/mcp_admin"
az ad sp create --id "$SAGE_APP_ID" 2>/dev/null || true
```

The identifier URIs serve different callers. `api://<SAGE_APP_ID>` is the
audience the BFF's on-behalf-of exchange and the deploy preflight token target.
The **https identities** exist for the MCP edge: the facade advertises
`https://<SAGE_PUBLIC_HOSTNAME>/Sage.Access` as its scope, and a standards MCP
client sends an RFC 8707 `resource` parameter with `/authorize`. Entra rejects
the request (`AADSTS9010010`, `invalid_target`) unless that parameter **is a
registered identifier URI** of the scope's app — same-origin is not enough; the
match is **byte-for-byte**, verified live on cor-prod. Each mount's
path-inserted protected-resource metadata steers its clients to the
path-carrying mount URI, so the mount forms are the resources clients actually
request. Two hard-won facts shape this set:

- **Trailing-slash forms cannot be registered** — Entra rejects them as invalid
  aliases (`Application alias 'https://…/' value is invalid`). A client that
  round-trips the advertised resource through a URL serializer turns a bare
  origin into `https://<host>/` (the serializer appends the slash to an empty
  path), a form Entra can then never match. This is why the edge's
  protected-resource metadata advertises **path-carrying mount URIs** (the
  path-inserted RFC 9728 documents) rather than the bare host: a URL with a
  path survives serialization byte-identically.
- Scope prefix and resource may be *different* identifier URIs of the same
  app; Entra requires only same-app resolution (e.g. scope
  `https://<host>/Sage.Access` with resource `https://<host>/mcp` is
  accepted).

The client independently requires the advertised resource to match the server
origin it connected to (RFC 9728), which is why the https identities derive
from the custom domain rather than a made-up URI. `--identifier-uris` is a
declarative full-set replace: declare all four together so a re-run cannot drop
any — and re-running the updated invocation is also how a *removed* URI (the
retired SSE-transport endpoint forms) is trimmed from a live tenant. The https
forms require their host under a tenant-verified domain; `az` fails loudly if
it is not. Tokens are unaffected either way — a v2 access token carries the
bare app-id GUID as its `aud` no matter which identifier form the scope used.

> The mount paths (`/mcp`, `/mcp_admin`) are protocol constants of the SAGE
> MCP Streamable HTTP surface, mirrored from the uvicorn mounts. Adding a new
> MCP mount means adding its identifier URI here and its path-inserted
> metadata operation at the APIM edge in the same change.

Expose the delegated scope and the app role, and pin the resource's access-token
version to **v2**. `requestedAccessTokenVersion`, `oauth2PermissionScopes`, and
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
      \"requestedAccessTokenVersion\": 2,
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

`requestedAccessTokenVersion: 2` pins the resource to **v2** access tokens. A
token minted for this audience via the `/.default` scope endpoint — which the
deploy preflight probe and the BFF on-behalf-of exchange both use — then carries
the tenant's v2.0 issuer, the issuer the APIM `validate-jwt` policy and the SAGE
backend validate against. The v1 `--resource` endpoint ignores this setting and
returns an `sts.windows.net` (v1) issuer, so it must not be used to reach SAGE.
Leaving the setting unset regresses a re-provisioned tenant to v1 tokens and a
401 at the edge.

## 2. SAGE access-provisioning group (CAS-ADR-044)

A single directory security group gates SAGE access: membership is **binary**
and **SAGE-wide**, enforced uniformly across every interactive surface — the
browser client and the public MCP client alike (CAS-ADR-044). Every
client-gating step below (§3, §4) assigns this **same** group, lookup-then-create;
whichever step runs first on a fresh tenant creates it, the others reconcile.

```bash
PROVISIONING_GROUP_NAME="${PROVISIONING_GROUP_NAME:-cas-sage-users}"
PROVISIONING_GROUP_ID="$(az ad group list --display-name "$PROVISIONING_GROUP_NAME" \
  --query '[0].id' -o tsv)"
if [ -z "$PROVISIONING_GROUP_ID" ]; then
  PROVISIONING_GROUP_ID="$(az ad group create --display-name "$PROVISIONING_GROUP_NAME" \
    --mail-nickname "$PROVISIONING_GROUP_NAME" --query id -o tsv)"
fi
```

### Provisioning users

Onboarding and offboarding are **membership edits** on the provisioning group —
ongoing operational churn, distinct from the one-time, idempotent bootstrap
above. Do not re-run this procedure to provision a user: a bootstrap re-run
reconciles directory objects, it does not manage people. A single membership
edit is the whole of onboarding or offboarding — no code change, no
redeployment:

```bash
az ad group member add --group cas-sage-users --member-id <USER_OBJECT_ID>
az ad group member remove --group cas-sage-users --member-id <USER_OBJECT_ID>
```

Resolve `<USER_OBJECT_ID>` at run time, for example
`az ad user show --id <USER_PRINCIPAL_NAME> --query id -o tsv`.

## 3. CAS BFF confidential-client registration

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
BFF_SP_ID="$(az ad sp create --id "$BFF_APP_ID" --query id -o tsv 2>/dev/null || \
  az ad sp show --id "$BFF_APP_ID" --query id -o tsv)"
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

Gate the BFF on the single provisioning group (CAS-ADR-044): assign the group to
its default-access role, then require app-role assignment — in that order, so
the gate never engages before its allowlist exists.

```bash
az rest --method POST \
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/${BFF_SP_ID}/appRoleAssignedTo" \
  --headers 'Content-Type=application/json' \
  --body "{
    \"principalId\": \"${PROVISIONING_GROUP_ID}\",
    \"resourceId\": \"${BFF_SP_ID}\",
    \"appRoleId\": \"<default-access app role id>\"
  }"

az rest --method PATCH \
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/${BFF_SP_ID}" \
  --headers 'Content-Type=application/json' \
  --body '{"appRoleAssignmentRequired": true}'
```

The gate fails closed on a fresh tenant: until the provisioning group has at
least one member, this switch refuses every sign-in, including the operator's —
populate initial membership as part of the same bootstrap sitting (see
*Provisioning users* in §2 above).

## 4. Public MCP client registration (auth-code + PKCE, no secret)

Create (or reuse) the MCP client as a **public client**: authorization-code +
PKCE, no client secret. This is the app id the DCR-compatibility facade's
`/register` operation echoes back to a standards-default MCP client, since Entra
offers no real Dynamic Client Registration (RFC 7591) — see the facade's own
design notes for how the discovery-and-registration leg is intercepted while
`authorize`/`token` stay pointed at Entra's real endpoints.

```bash
MCP_CLIENT_APP_ID="$(az ad app list --display-name cas-mcp-client --query '[0].appId' -o tsv)"
if [ -z "$MCP_CLIENT_APP_ID" ]; then
  MCP_CLIENT_APP_ID="$(az ad app create --display-name cas-mcp-client \
    --sign-in-audience AzureADMyOrg \
    --public-client-redirect-uris "<MCP_CLIENT_REDIRECT_URI>" "http://localhost/callback" \
    --query appId -o tsv)"
else
  az ad app update --id "$MCP_CLIENT_APP_ID" \
    --public-client-redirect-uris "<MCP_CLIENT_REDIRECT_URI>" "http://localhost/callback"
fi
MCP_CLIENT_SP_ID="$(az ad sp create --id "$MCP_CLIENT_APP_ID" --query id -o tsv 2>/dev/null || \
  az ad sp show --id "$MCP_CLIENT_APP_ID" --query id -o tsv)"
```

`--public-client-redirect-uris` registers the public-client redirect-uri
platform — never `--web-redirect-uris`, which implies a confidential client that
would need a secret the PKCE flow does not use. The set carries two URIs: the
env-resolved `<MCP_CLIENT_REDIRECT_URI>` primary and the `http://localhost/callback`
loopback a browser-context Desktop client uses for its auth-code/PKCE callback.
The flag is a declarative full-set replace, so registering both together keeps a
re-run from dropping the loopback.

Grant the same delegated `Sage.Access` scope the BFF holds, then admin-consent
it — permission add and consent are identical to the BFF step above:

```bash
az ad app permission add --id "$MCP_CLIENT_APP_ID" \
  --api "$SAGE_APP_ID" \
  --api-permissions "${ACCESS_SCOPE_ID}=Scope"
az ad app permission admin-consent --id "$MCP_CLIENT_APP_ID"
```

Finally, gate the client on the single provisioning group (CAS-ADR-044): require
app-role assignment on its service principal, then assign the group to its
default-access app role — the same body shape applied to the browser client,
though posted to the `appRoleAssignments` collection rather than
`appRoleAssignedTo`. Documentation for `appRoleAssignments` describes it as the
*principal's* own collection, which would suggest a POST here requires
`principalId` to equal this SP — it doesn't turn out that way in practice: this
call was verified against a live tenant, and Graph accepts the identical body
(`principalId` = group, `resourceId` = this SP) on either collection. Don't
switch this to `appRoleAssignedTo` on the strength of the documented contract
alone without re-verifying live; the two collections aren't proven equivalent in
the reverse direction.

```bash
az rest --method PATCH \
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/${MCP_CLIENT_SP_ID}" \
  --headers 'Content-Type=application/json' \
  --body '{"appRoleAssignmentRequired": true}'

az rest --method POST \
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/${MCP_CLIENT_SP_ID}/appRoleAssignments" \
  --headers 'Content-Type=application/json' \
  --body "{
    \"principalId\": \"${PROVISIONING_GROUP_ID}\",
    \"resourceId\": \"${MCP_CLIENT_SP_ID}\",
    \"appRoleId\": \"<default-access app role id>\"
  }"
```

The `appRoleId` here is the well-known all-zero Microsoft Graph sentinel used to
assign a principal to an application's default access when the application
defines no custom app roles; the codified script builds it from repeated `0`s
rather than a literal so the durable script carries no GUID-shaped literal.

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
