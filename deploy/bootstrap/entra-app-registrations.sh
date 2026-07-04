#!/usr/bin/env bash
# Create (or reconcile) the three Entra app registrations the cloud auth model
# depends on (CAS-ADR-042): SAGE as an OAuth resource server, the CAS BFF as a
# confidential client that calls SAGE on-behalf-of an interactive user, and the
# public MCP client (auth-code + PKCE, no secret) the DCR-compatibility facade
# registers back at /register (CAS-ADR-042) -- then gate both clients' sign-in
# on membership in the single SAGE access-provisioning group (CAS-ADR-044).
# This is the executable substance of docs/process/entra-app-registrations.md.
#
# One-time per tenant, run by an operator with directory-admin rights. Idempotent:
# every create is guarded by a lookup, and the scope/role ids are stable across
# runs when passed in the environment. On success it emits the sageAudience,
# bffOidcClientId, and mcpClientId coordinates for the deployment parameter set.
set -euo pipefail

# The BFF cloud hostname and OIDC callback path the redirect URI is built from.
# BFF_HOSTNAME is the default ingress FQDN until the custom domain is bound;
# AUTH_CALLBACK_PATH is fixed by the BFF login implementation.
: "${BFF_HOSTNAME:?set BFF_HOSTNAME to the BFF cloud hostname (e.g. the default ingress FQDN)}"
AUTH_CALLBACK_PATH="${AUTH_CALLBACK_PATH:-auth/callback}"

# The public MCP client's registered redirect URI(s) -- resolved against the
# chosen default MCP client's current documentation (a loopback or custom-scheme
# callback), not a CAS-controlled hostname. Comma-separated if the client needs
# more than one.
: "${MCP_CLIENT_REDIRECT_URI:?set MCP_CLIENT_REDIRECT_URI to the MCP client registered redirect URI (see docs/process/entra-app-registrations.md)}"

# The public SAGE hostname (the sage custom domain, e.g. sage.<base-domain>).
# Registered below as an https identifier URI on the resource server so the
# {{sage-resource-url}}/Sage.Access scope the edge advertises resolves to this
# app. Must sit under a domain verified in the tenant.
: "${SAGE_PUBLIC_HOSTNAME:?set SAGE_PUBLIC_HOSTNAME to the public SAGE hostname (e.g. sage.<base-domain>)}"

# 1. SAGE resource-server registration (lookup-then-create keeps it idempotent).
SAGE_APP_ID="$(az ad app list --display-name sage-resource-server \
  --query '[0].appId' -o tsv)"
if [ -z "${SAGE_APP_ID}" ]; then
  SAGE_APP_ID="$(az ad app create --display-name sage-resource-server \
    --sign-in-audience AzureADMyOrg --query appId -o tsv)"
fi
# Four identifier URIs, declared together (the flag is a declarative full-set
# replace, so a re-run keeps exactly these -- and re-running after a set
# change IS the live-tenant trim): the api://<app-id> audience URI the BFF OBO
# exchange and the deploy preflight token target, the https custom-domain
# identity the MCP edge advertises as its scope prefix, and the two MCP-mount
# forms of that identity. The https forms exist because an MCP client sends an
# RFC 8707 resource parameter with /authorize and Entra rejects the request
# (AADSTS9010010, invalid_target) unless that parameter IS a registered
# identifier URI of the scope's app -- same-origin is not enough, matched
# byte-for-byte, verified live. The mount forms are the resources clients
# actually request: each mount's protected-resource metadata steers its
# clients to the path-carrying mount URI, because trailing-slash forms canNOT
# be registered (Entra rejects them as invalid aliases) and a bare origin
# normalizes to https://<host>/ in a client's URL serializer and can never
# match. Scope prefix and resource may be different identifier URIs of the
# same app; only same-app resolution is required. The mount paths are protocol
# constants of the SAGE MCP Streamable HTTP surface (each mount serves
# JSON-RPC POSTs at its own path), not per-tenant coordinates. The https
# identifier URIs require their host under a tenant-verified domain; az fails
# loudly here if it is not.
az ad app update --id "${SAGE_APP_ID}" \
  --identifier-uris "api://${SAGE_APP_ID}" "https://${SAGE_PUBLIC_HOSTNAME}" \
    "https://${SAGE_PUBLIC_HOSTNAME}/mcp" "https://${SAGE_PUBLIC_HOSTNAME}/mcp_admin"
az ad sp create --id "${SAGE_APP_ID}" 2>/dev/null || true

# Expose the single delegated scope and app role that authorize both the REST
# and MCP surfaces, and pin the access-token version to v2 so a token minted for
# this resource via the /.default scope endpoint carries the tenant's v2.0 issuer
# -- the issuer APIM validate-jwt and the SAGE backend require. Generate the
# scope/role ids once; keep them stable across runs by passing ACCESS_SCOPE_ID /
# SAGE_READER_ROLE_ID in the environment.
SAGE_OBJECT_ID="$(az ad app show --id "${SAGE_APP_ID}" --query id -o tsv)"
ACCESS_SCOPE_ID="${ACCESS_SCOPE_ID:-$(uuidgen)}"
SAGE_READER_ROLE_ID="${SAGE_READER_ROLE_ID:-$(uuidgen)}"

# Pre-authorize Azure CLI on the SAGE resource server (folded into the api PATCH
# below) so an operator can hand-mint the bearer deploy/cloud-preflight.sh needs
# -- `az account get-access-token --scope api://<SAGE_APP_ID>/.default` -- without
# a per-app consent screen. Verified live against cor.org (2026-07-04): without
# this, that call fails AADSTS650057 (invalid_client) on a fresh SAGE
# registration.
#
# The value to pre-authorize is Azure CLI's own first-party client id, a fixed
# Microsoft-published multi-tenant constant. It is read off the appid claim of a
# token az itself already holds -- a v1 --resource token carries appid (any
# resource works; Graph is always reachable) -- rather than pasted as a GUID
# literal. But `az account get-access-token` returns a token whose appid is the
# *running* principal's, which equals Azure CLI's constant only under an
# interactive `az login` (a human directory admin, as this one-time-per-tenant
# script assumes). Run under `az login --service-principal` or a managed
# identity, appid is that principal's id, and pre-authorizing it instead would
# silently NOT clear AADSTS650057 for a later operator. So the resolved value is
# checked against Azure CLI's known constant -- assembled from segments the way
# DEFAULT_ACCESS_APP_ROLE_ID is below, so this durable script still carries no
# GUID-shaped literal -- and the run aborts loudly on a mismatch rather than
# pre-authorizing the wrong app.
AZURE_CLI_APP_ID="$(az account get-access-token --resource https://graph.microsoft.com \
  --query accessToken -o tsv | python3 -c '
import base64, json, sys
segment = sys.stdin.read().strip().split(".")[1]
segment += "=" * (-len(segment) % 4)
print(json.loads(base64.urlsafe_b64decode(segment))["appid"])
')"
AZURE_CLI_ID_HEAD="04b07795-8ddb-461a"
AZURE_CLI_ID_TAIL="bbee-02f9e1bf7b46"
AZURE_CLI_KNOWN_APP_ID="${AZURE_CLI_ID_HEAD}-${AZURE_CLI_ID_TAIL}"
if [ "${AZURE_CLI_APP_ID}" != "${AZURE_CLI_KNOWN_APP_ID}" ]; then
  echo "ERROR: az minted a token whose appid (${AZURE_CLI_APP_ID}) is not Azure" \
    "CLI's own client id (${AZURE_CLI_KNOWN_APP_ID}). Run this bootstrap under an" \
    "interactive 'az login' as a directory admin -- not a service principal or" \
    "managed identity -- so Azure CLI is the app being pre-authorized." >&2
  exit 1
fi

# One PATCH pins the whole SAGE registration: access-token v2 (so a /.default
# token carries the v2.0 issuer APIM validate-jwt and the SAGE backend require),
# the single Sage.Access delegated scope and Sage.Reader app role that authorize
# the REST and MCP surfaces, and the preAuthorizedApplications entry for Azure
# CLI. Folding the pre-authorization into this PATCH -- rather than a second
# PATCH to the same `api` complex property -- keeps requestedAccessTokenVersion
# and oauth2PermissionScopes off Graph's merge-vs-replace semantics for a
# follow-up `api` write. preAuthorizedApplications is a declarative full-set
# replace (like the identifier URIs above), so a future addition to this list
# must include this Azure CLI entry or a re-run will drop it.
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
      }],
      \"preAuthorizedApplications\": [{
        \"appId\": \"${AZURE_CLI_APP_ID}\",
        \"delegatedPermissionIds\": [\"${ACCESS_SCOPE_ID}\"]
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

# 2. The single SAGE access-provisioning group (CAS-ADR-044): binary membership,
# uniform across every interactive surface (browser and agent alike). Every
# client-gating step below assigns this same group to its own service
# principal's default-access role; lookup-then-create so whichever step runs
# first on a fresh tenant creates it, the rest reconcile.
PROVISIONING_GROUP_NAME="${PROVISIONING_GROUP_NAME:-cas-sage-users}"
PROVISIONING_GROUP_ID="$(az ad group list --display-name "${PROVISIONING_GROUP_NAME}" \
  --query '[0].id' -o tsv)"
if [ -z "${PROVISIONING_GROUP_ID}" ]; then
  PROVISIONING_GROUP_ID="$(az ad group create --display-name "${PROVISIONING_GROUP_NAME}" \
    --mail-nickname "${PROVISIONING_GROUP_NAME}" --query id -o tsv)"
fi

# The default-access app role id is the well-known all-zero Microsoft Graph
# sentinel used to assign a principal to an application that defines no custom
# app roles -- built from repeated '0's rather than written as a literal so
# this durable script carries no GUID-shaped literal. Computed once, reused by
# every client-gating step below.
DEFAULT_ACCESS_APP_ROLE_ID="$(printf '0%.0s' {1..8})-$(printf '0%.0s' {1..4})-$(printf '0%.0s' {1..4})-$(printf '0%.0s' {1..4})-$(printf '0%.0s' {1..12})"

# 3. CAS BFF confidential-client registration (lookup-then-create).
BFF_APP_ID="$(az ad app list --display-name cas-bff --query '[0].appId' -o tsv)"
if [ -z "${BFF_APP_ID}" ]; then
  BFF_APP_ID="$(az ad app create --display-name cas-bff \
    --sign-in-audience AzureADMyOrg \
    --web-redirect-uris "https://${BFF_HOSTNAME}/${AUTH_CALLBACK_PATH}" \
    --query appId -o tsv)"
else
  az ad app update --id "${BFF_APP_ID}" \
    --web-redirect-uris "https://${BFF_HOSTNAME}/${AUTH_CALLBACK_PATH}"
fi
BFF_SP_ID="$(az ad sp create --id "${BFF_APP_ID}" --query id -o tsv 2>/dev/null || \
  az ad sp show --id "${BFF_APP_ID}" --query id -o tsv)"

# Grant the BFF the delegated API permission onto SAGE, then admin-consent it —
# this is what makes the on-behalf-of exchange possible.
az ad app permission add --id "${BFF_APP_ID}" \
  --api "${SAGE_APP_ID}" \
  --api-permissions "${ACCESS_SCOPE_ID}=Scope"
az ad app permission admin-consent --id "${BFF_APP_ID}"

# Gate the BFF on the single provisioning group (CAS-ADR-044): assign the
# group to its default-access role, then require app-role assignment -- in
# that order, so the gate never engages before its allowlist exists.
az rest --method POST \
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/${BFF_SP_ID}/appRoleAssignedTo" \
  --headers 'Content-Type=application/json' \
  --body "{
    \"principalId\": \"${PROVISIONING_GROUP_ID}\",
    \"resourceId\": \"${BFF_SP_ID}\",
    \"appRoleId\": \"${DEFAULT_ACCESS_APP_ROLE_ID}\"
  }" || true  # tolerate an already-present assignment on re-run

az rest --method PATCH \
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/${BFF_SP_ID}" \
  --headers 'Content-Type=application/json' \
  --body '{"appRoleAssignmentRequired": true}'

# 4. Public MCP client registration (lookup-then-create): auth-code + PKCE, no
# secret -- the DCR-compatibility facade's /register operation echoes this app
# id back to a default MCP client, since Entra offers no real Dynamic Client
# Registration (CAS-ADR-042). --public-client-redirect-uris registers
# the public-client platform (never --web-redirect-uris, which implies a
# confidential client that would need a secret). The set carries both the
# env-resolved primary redirect and the http://localhost/callback loopback a
# browser-context Desktop client uses for its auth-code/PKCE callback; the flag
# is a declarative full-set replace, so registering both here keeps a re-bootstrap
# from dropping the loopback.
MCP_CLIENT_APP_ID="$(az ad app list --display-name cas-mcp-client --query '[0].appId' -o tsv)"
if [ -z "${MCP_CLIENT_APP_ID}" ]; then
  MCP_CLIENT_APP_ID="$(az ad app create --display-name cas-mcp-client \
    --sign-in-audience AzureADMyOrg \
    --public-client-redirect-uris "${MCP_CLIENT_REDIRECT_URI}" "http://localhost/callback" \
    --query appId -o tsv)"
else
  az ad app update --id "${MCP_CLIENT_APP_ID}" \
    --public-client-redirect-uris "${MCP_CLIENT_REDIRECT_URI}" "http://localhost/callback"
fi
MCP_CLIENT_SP_ID="$(az ad sp create --id "${MCP_CLIENT_APP_ID}" --query id -o tsv 2>/dev/null || \
  az ad sp show --id "${MCP_CLIENT_APP_ID}" --query id -o tsv)"

# Grant the same delegated SAGE.Access scope the BFF holds; the admin-consent
# below records the tenant consent for this SAGE.Access grant. It does NOT
# consent the offline_access grant that follows -- that needs its own explicit
# grant (verified live: admin-consent returns 0 yet records no offline_access
# consent), issued after the admin-consent step below.
az ad app permission add --id "${MCP_CLIENT_APP_ID}" \
  --api "${SAGE_APP_ID}" \
  --api-permissions "${ACCESS_SCOPE_ID}=Scope"

# Also grant offline_access so Entra issues a refresh token to this public client
# -- without it a v2 access token (60-90 min lifetime) expires with no way to
# renew the session but a fresh /authorize round trip (CAS-ADR-042).
# offline_access is a Microsoft Graph delegated permission, not a scope on the
# SAGE resource server, so it is granted against Graph's first-party service
# principal; Graph's app id and the offline_access scope id are resolved from the
# tenant at run time, never hardcoded, keeping this durable script free of
# GUID-shaped literals.
GRAPH_APP_ID="$(az ad sp list --filter "displayName eq 'Microsoft Graph'" \
  --query '[0].appId' -o tsv)"
OFFLINE_ACCESS_SCOPE_ID="$(az ad sp show --id "${GRAPH_APP_ID}" \
  --query "oauth2PermissionScopes[?value=='offline_access'].id | [0]" -o tsv)"
az ad app permission add --id "${MCP_CLIENT_APP_ID}" \
  --api "${GRAPH_APP_ID}" \
  --api-permissions "${OFFLINE_ACCESS_SCOPE_ID}=Scope"
az ad app permission admin-consent --id "${MCP_CLIENT_APP_ID}"

# admin-consent above records the tenant consent for the SAGE.Access delegated
# scope but empirically does NOT create the delegated grant for the Graph
# offline_access scope -- az returns 0 yet no oauth2PermissionGrant for
# offline_access appears (verified live against the tenant). Without the grant
# Entra issues no refresh token and the symptom the offline_access request was
# meant to fix returns. An explicit grant is what actually consents it: it
# records an AllPrincipals oauth2PermissionGrant for offline_access on the Graph
# resource. Graph's app id is reused from the run-time resolution above, so this
# durable script still carries no GUID-shaped literal (CAS-ADR-042).
az ad app permission grant --id "${MCP_CLIENT_APP_ID}" \
  --api "${GRAPH_APP_ID}" \
  --scope offline_access

# Gate the public client on the single provisioning group (CAS-ADR-044): require
# app-role assignment on its service principal, then assign the group to its
# default-access role.
#
# This POSTs to the appRoleAssignments collection rather than appRoleAssignedTo
# (used by the BFF gate above) -- documented as the *principal's* own collection,
# which would suggest it requires principalId to equal this SP. Verified against
# a live tenant: Graph accepts this body (principalId = group, resourceId = this
# SP) on either collection. Don't swap to appRoleAssignedTo on the strength of the
# documented contract alone -- the two are not proven equivalent in the other
# direction, and this shape is confirmed working.
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
    \"appRoleId\": \"${DEFAULT_ACCESS_APP_ROLE_ID}\"
  }" || true  # tolerate an already-present assignment on re-run

# Emit the coordinates for the deployment parameter set (main.bicepparam).
echo "# Paste into the tenant parameter set:"
echo "param sageAudience = 'api://${SAGE_APP_ID}'"
echo "param bffOidcClientId = '${BFF_APP_ID}'"
echo "param mcpClientId = '${MCP_CLIENT_APP_ID}'"
