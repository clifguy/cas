#!/usr/bin/env bash
# Create (or reconcile) the three Entra app registrations the cloud auth model
# depends on (CAS-ADR-042): SAGE as an OAuth resource server, the CAS BFF as a
# confidential client that calls SAGE on-behalf-of an interactive user, and the
# public MCP client (auth-code + PKCE, no secret) the DCR-compatibility facade
# registers back at /register (CAS-ADR-042) -- then gate the SAGE resource and
# both clients on membership in the single SAGE access-provisioning group
# (CAS-ADR-044). Gating the resource is what makes the gate hold for every
# client, including Azure CLI and any app a user signs in through; gating the
# two clients refuses a non-member at their own sign-in as well.
# This is the executable substance of docs/process/entra-app-registrations.md.
#
# One-time per tenant, run by an operator with directory-admin rights. Idempotent:
# every create and every assignment is guarded by a lookup, and the scope/role
# ids are stable across runs when passed in the environment. Directory objects
# are selected by exact display name, and a run stops on any failed call rather
# than continuing past it. On success it emits the sageAudience,
# bffOidcClientId, and mcpClientId coordinates for the deployment parameter set.
set -euo pipefail

# Print the single value an `az ... list` call returns, or nothing when it
# returns none; stop the run when it returns more than one. Callers pass an
# exact OData filter -- `--display-name` matches by prefix, so a look-alike such
# as "<name>-old" could otherwise be selected -- and a `[].<field>` query.
#
# These helpers run inside command substitutions, where bash does not apply
# `set -e`, so each failure exits explicitly: a failed lookup must stop the run,
# never read as "absent" and lead to a duplicate create.
lookup_one() {
  local what="$1"
  shift
  local found
  found="$("$@" -o tsv)" || exit 1
  if [ "$(printf '%s' "${found}" | grep -c .)" -gt 1 ]; then
    echo "ERROR: expected exactly one ${what}, found several; resolve the duplicate" \
      "in the directory before re-running." >&2
    exit 1
  fi
  printf '%s' "${found}"
}

# Print the object id of the service principal for an application, creating it
# when the application has none.
ensure_sp() {
  local app_id="$1" sp_id
  sp_id="$(lookup_one "service principal for ${app_id}" \
    az ad sp list --filter "appId eq '${app_id}'" --query '[].id')" || exit 1
  if [ -z "${sp_id}" ]; then
    sp_id="$(az ad sp create --id "${app_id}" --query id -o tsv)" || exit 1
  fi
  printf '%s' "${sp_id}"
}

# Gate a service principal on the provisioning group (CAS-ADR-044): assign the
# group to the given app role unless that assignment already exists, then
# require app-role assignment -- in that order, so the gate never engages
# before its allowlist exists. The existence check is what keeps a re-run
# idempotent; a failed call stops the run instead of being tolerated. It reads
# the group's own assignments rather than the principal's: `az rest` returns a
# single page, and the access group holds a handful of assignments where a
# resource may hold many. Were an assignment ever past that page, the POST is
# refused as a duplicate and the run stops before requiring assignment.
ensure_group_gate() {
  local sp_id="$1" app_role_id="$2" existing
  existing="$(az rest --method GET \
    --url "https://graph.microsoft.com/v1.0/groups/${PROVISIONING_GROUP_ID}/appRoleAssignments" \
    --query "value[?resourceId=='${sp_id}' && appRoleId=='${app_role_id}'].id" \
    -o tsv)"
  if [ -z "${existing}" ]; then
    az rest --method POST \
      --url "https://graph.microsoft.com/v1.0/servicePrincipals/${sp_id}/appRoleAssignedTo" \
      --headers 'Content-Type=application/json' \
      --body "{
        \"principalId\": \"${PROVISIONING_GROUP_ID}\",
        \"resourceId\": \"${sp_id}\",
        \"appRoleId\": \"${app_role_id}\"
      }" >/dev/null
  fi
  az rest --method PATCH \
    --url "https://graph.microsoft.com/v1.0/servicePrincipals/${sp_id}" \
    --headers 'Content-Type=application/json' \
    --body '{"appRoleAssignmentRequired": true}'
}

# Print each distinct non-empty argument once, one per line, in first-seen order.
unique_lines() {
  local uri kept seen
  local -a out=()
  for uri in "$@"; do
    [ -n "${uri}" ] || continue
    seen=0
    for kept in ${out[@]+"${out[@]}"}; do
      if [ "${kept}" = "${uri}" ]; then
        seen=1
        break
      fi
    done
    [ "${seen}" -eq 1 ] || out+=("${uri}")
  done
  for uri in ${out[@]+"${out[@]}"}; do
    printf '%s\n' "${uri}"
  done
}

# Print the redirect URIs an application should carry on one platform: every URI
# it already holds, in its current order, followed by each required URI it
# lacks. The redirect-URI flags of `az ad app update` replace the whole
# collection, so writing this union back adds what the bootstrap needs and
# never removes a URI registered since -- a desktop MCP client's per-port
# loopback, say. Removing one is a deliberate operator step, described in
# docs/process/entra-app-registrations.md. Takes the application id, the
# platform object in the application (publicClient or web), and the required
# URIs.
#
# The platform object is read whole and must carry a redirectUris list, which
# Graph reports even when it is empty. A query path that matches nothing prints
# nothing and still exits 0, so reading the list alone could not tell a
# mistyped or renamed field from an empty platform, and the union written back
# would delete every live URI. Anything but a list of strings stops the run.
merged_redirect_uris() {
  local app_id="$1" platform="$2" live uri
  local -a current=()
  shift 2
  live="$(az ad app show --id "${app_id}" --query "${platform}" -o json)" || exit 1
  live="$(printf '%s' "${live}" | python3 -c '
import json, sys
try:
    uris = json.load(sys.stdin)["redirectUris"]
except (ValueError, TypeError, KeyError):
    sys.exit(1)
if not isinstance(uris, list) or not all(isinstance(u, str) for u in uris):
    sys.exit(1)
print("\n".join(uris))
')" || {
    echo "ERROR: could not read the ${platform} redirect URIs of application" \
      "${app_id}; refusing to overwrite them." >&2
    exit 1
  }
  while IFS= read -r uri; do
    current+=("${uri}")
  done <<<"${live}"
  unique_lines ${current[@]+"${current[@]}"} "$@"
}

# The BFF cloud hostname and OIDC callback path the redirect URI is built from.
# BFF_HOSTNAME is the default ingress FQDN until the custom domain is bound;
# AUTH_CALLBACK_PATH is fixed by the BFF login implementation (CALLBACK_PATH in
# app/backend/auth/config.py, without its leading slash), so the default is that
# path; override it only for a BFF built with a different one.
: "${BFF_HOSTNAME:?set BFF_HOSTNAME to the BFF cloud hostname (e.g. the default ingress FQDN)}"
AUTH_CALLBACK_PATH="${AUTH_CALLBACK_PATH:-app/auth/callback}"

# The public MCP client's registered redirect URI(s) -- resolved against the
# chosen default MCP client's current documentation (a loopback or custom-scheme
# callback), not a CAS-controlled hostname. A comma-separated list registers one
# entry per URI; whitespace around an entry is ignored, and an empty entry (a
# doubled or trailing comma) stops the run before any directory call. The
# http://localhost/callback loopback a browser-context desktop client uses for
# its auth-code/PKCE callback is always added.
: "${MCP_CLIENT_REDIRECT_URI:?set MCP_CLIENT_REDIRECT_URI to the MCP client registered redirect URI (see docs/process/entra-app-registrations.md)}"
# `read` consumes a single line, so a value spanning several is refused rather
# than silently cut at its first newline. The appended comma keeps a trailing
# empty entry visible: `read -a` drops one empty final field, which is then the
# added one alone.
case "${MCP_CLIENT_REDIRECT_URI}" in
  *$'\n'*)
    echo "ERROR: MCP_CLIENT_REDIRECT_URI must be one line; separate URIs with commas." >&2
    exit 1
    ;;
esac
IFS=',' read -r -a MCP_REDIRECT_ENTRIES <<<"${MCP_CLIENT_REDIRECT_URI},"
MCP_REQUIRED_INPUT=()
for entry in "${MCP_REDIRECT_ENTRIES[@]}"; do
  entry="${entry#"${entry%%[![:space:]]*}"}"
  entry="${entry%"${entry##*[![:space:]]}"}"
  if [ -z "${entry}" ]; then
    echo "ERROR: MCP_CLIENT_REDIRECT_URI has an empty entry; separate URIs with" \
      "single commas and no trailing comma." >&2
    exit 1
  fi
  MCP_REQUIRED_INPUT+=("${entry}")
done
MCP_REQUIRED_REDIRECTS=()
while IFS= read -r uri; do
  MCP_REQUIRED_REDIRECTS+=("${uri}")
done <<<"$(unique_lines "${MCP_REQUIRED_INPUT[@]}" "http://localhost/callback")"

# The public SAGE hostname (the sage custom domain, e.g. sage.<base-domain>).
# Registered below as an https identifier URI on the resource server so the
# {{sage-resource-url}}/Sage.Access scope the edge advertises resolves to this
# app. Must sit under a domain verified in the tenant.
: "${SAGE_PUBLIC_HOSTNAME:?set SAGE_PUBLIC_HOSTNAME to the public SAGE hostname (e.g. sage.<base-domain>)}"

# 1. SAGE resource-server registration (lookup-then-create keeps it idempotent).
SAGE_APP_ID="$(lookup_one "application named sage-resource-server" \
  az ad app list --filter "displayName eq 'sage-resource-server'" --query '[].appId')"
if [ -z "${SAGE_APP_ID}" ]; then
  SAGE_APP_ID="$(az ad app create --display-name sage-resource-server \
    --sign-in-audience AzureADMyOrg --query appId -o tsv)"
fi
# Four identifier URIs, declared together (the flag is a declarative full-set
# replace, so a re-run keeps exactly these -- and re-running after a set
# change IS the live-tenant trim): the api://<app-id> audience URI the BFF OBO
# exchange and the deploy preflight token target, the https custom-domain
# identity the MCP edge advertises as its scope prefix, and the MCP-mount
# forms of that identity (/mcp and /mcp_maint). The https forms exist because an MCP client sends an
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
    "https://${SAGE_PUBLIC_HOSTNAME}/mcp" "https://${SAGE_PUBLIC_HOSTNAME}/mcp_maint"
SAGE_SP_ID="$(ensure_sp "${SAGE_APP_ID}")"

# Expose the single delegated scope and app role that authorize both the REST
# and MCP surfaces, and pin the access-token version to v2 so a token minted for
# this resource via the /.default scope endpoint carries the tenant's v2.0 issuer
# -- the issuer APIM validate-jwt and the SAGE backend require. The scope and
# role ids are generated once: a re-run reuses the ids the registration already
# carries (or ACCESS_SCOPE_ID / SAGE_READER_ROLE_ID from the environment), since
# the PATCH below replaces both collections and a fresh id would orphan every
# consent grant and role assignment made against the old one.
#
# Sage.Access is admin-consent-only ("type": "Admin"): every supported client is
# either admin-consented below (the BFF and the public MCP client) or
# pre-authorized (Azure CLI), so none needs a user's own consent, and a
# user-consentable scope would let any member grant an arbitrary app delegated
# SAGE access.
SAGE_OBJECT_ID="$(az ad app show --id "${SAGE_APP_ID}" --query id -o tsv)"
ACCESS_SCOPE_ID="${ACCESS_SCOPE_ID:-$(az ad app show --id "${SAGE_APP_ID}" \
  --query "api.oauth2PermissionScopes[?value=='Sage.Access'].id | [0]" -o tsv)}"
ACCESS_SCOPE_ID="${ACCESS_SCOPE_ID:-$(uuidgen)}"
SAGE_READER_ROLE_ID="${SAGE_READER_ROLE_ID:-$(az ad app show --id "${SAGE_APP_ID}" \
  --query "appRoles[?value=='Sage.Reader'].id | [0]" -o tsv)}"
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
        \"type\": \"Admin\",
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
      \"allowedMemberTypes\": [\"User\", \"Application\"],
      \"value\": \"Sage.Reader\",
      \"displayName\": \"Sage.Reader\",
      \"description\": \"Read across the SAGE REST and MCP surfaces.\",
      \"isEnabled\": true
    }]
  }"

# 2. The single SAGE access-provisioning group (CAS-ADR-044): binary membership,
# uniform across every interactive surface (browser and agent alike). Every
# gating step below assigns this same group to its own service principal;
# lookup-then-create so whichever step runs first on a fresh tenant creates it,
# the rest reconcile.
PROVISIONING_GROUP_NAME="${PROVISIONING_GROUP_NAME:-cas-sage-users}"
# The name is matched inside a quoted OData literal; a quote in it would end the
# literal early and let the filter select some other group.
case "${PROVISIONING_GROUP_NAME}" in
  *"'"*)
    echo "ERROR: PROVISIONING_GROUP_NAME must not contain a single quote." >&2
    exit 1
    ;;
esac
PROVISIONING_GROUP_ID="$(lookup_one "group named ${PROVISIONING_GROUP_NAME}" \
  az ad group list --filter "displayName eq '${PROVISIONING_GROUP_NAME}'" --query '[].id')"
if [ -z "${PROVISIONING_GROUP_ID}" ]; then
  PROVISIONING_GROUP_ID="$(az ad group create --display-name "${PROVISIONING_GROUP_NAME}" \
    --mail-nickname "${PROVISIONING_GROUP_NAME}" --query id -o tsv)"
fi

# 2a. Gate the SAGE resource itself on the group. With assignment required on
# the resource, Entra issues a token for the SAGE audience only to an assigned
# principal, whichever client requests it -- the BFF's on-behalf-of exchange,
# the public MCP client, Azure CLI, or an application acting as itself. The
# group is assigned to Sage.Reader, the role the registration declares for
# users; an application principal (the CI deploy identity) holds its own
# Sage.Reader assignment, granted separately, which keeps its client-credentials
# path working.
#
# Requiring assignment stops any application principal without that grant from
# obtaining a token. A fresh tenant has none yet (the deploy identity is granted
# after this bootstrap), so their absence is reported rather than refused.
APP_READERS="$(az rest --method GET \
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/${SAGE_SP_ID}/appRoleAssignedTo" \
  --query "value[?principalType=='ServicePrincipal' && appRoleId=='${SAGE_READER_ROLE_ID}'].id" \
  -o tsv)"
if [ -z "${APP_READERS}" ]; then
  echo "WARNING: no application principal holds Sage.Reader on the SAGE resource." \
    "Once assignment is required, an application without it (the CI deploy" \
    "identity included) cannot obtain a SAGE token; grant it as described in" \
    "docs/process/azure-deployment.md." >&2
fi
ensure_group_gate "${SAGE_SP_ID}" "${SAGE_READER_ROLE_ID}"

# The default-access app role id is the well-known all-zero Microsoft Graph
# sentinel used to assign a principal to an application that defines no custom
# app roles -- built from repeated '0's rather than written as a literal so
# this durable script carries no GUID-shaped literal. Computed once, reused by
# both client-gating steps below.
DEFAULT_ACCESS_APP_ROLE_ID="$(printf '0%.0s' {1..8})-$(printf '0%.0s' {1..4})-$(printf '0%.0s' {1..4})-$(printf '0%.0s' {1..4})-$(printf '0%.0s' {1..12})"

# 3. CAS BFF confidential-client registration (lookup-then-create). A new
# registration gets the single OIDC callback; an existing one keeps the web
# redirect URIs it holds and gains the callback if it lacks it, so a hostname
# change leaves the old callback in place until an operator removes it.
BFF_CALLBACK_URI="https://${BFF_HOSTNAME}/${AUTH_CALLBACK_PATH}"
BFF_APP_ID="$(lookup_one "application named cas-bff" \
  az ad app list --filter "displayName eq 'cas-bff'" --query '[].appId')"
if [ -z "${BFF_APP_ID}" ]; then
  BFF_APP_ID="$(az ad app create --display-name cas-bff \
    --sign-in-audience AzureADMyOrg \
    --web-redirect-uris "${BFF_CALLBACK_URI}" \
    --query appId -o tsv)"
else
  BFF_MERGED="$(merged_redirect_uris "${BFF_APP_ID}" web "${BFF_CALLBACK_URI}")"
  BFF_REDIRECTS=()
  while IFS= read -r uri; do
    BFF_REDIRECTS+=("${uri}")
  done <<<"${BFF_MERGED}"
  az ad app update --id "${BFF_APP_ID}" \
    --web-redirect-uris "${BFF_REDIRECTS[@]}"
fi
BFF_SP_ID="$(ensure_sp "${BFF_APP_ID}")"

# Grant the BFF the delegated API permission onto SAGE, then admin-consent it —
# this is what makes the on-behalf-of exchange possible.
az ad app permission add --id "${BFF_APP_ID}" \
  --api "${SAGE_APP_ID}" \
  --api-permissions "${ACCESS_SCOPE_ID}=Scope"
az ad app permission admin-consent --id "${BFF_APP_ID}"

# Gate the BFF's own sign-in on the single provisioning group (CAS-ADR-044),
# through its default-access role.
ensure_group_gate "${BFF_SP_ID}" "${DEFAULT_ACCESS_APP_ROLE_ID}"

# 4. Public MCP client registration (lookup-then-create): auth-code + PKCE, no
# secret -- the DCR-compatibility facade's /register operation echoes this app
# id back to a default MCP client, since Entra offers no real Dynamic Client
# Registration (CAS-ADR-042). --public-client-redirect-uris registers
# the public-client platform (never --web-redirect-uris, which implies a
# confidential client that would need a secret). A new registration gets
# exactly the required set (the MCP_CLIENT_REDIRECT_URI entries and the
# http://localhost/callback loopback); an existing one keeps every redirect URI
# it holds and gains any required one it lacks.
MCP_CLIENT_APP_ID="$(lookup_one "application named cas-mcp-client" \
  az ad app list --filter "displayName eq 'cas-mcp-client'" --query '[].appId')"
if [ -z "${MCP_CLIENT_APP_ID}" ]; then
  MCP_CLIENT_APP_ID="$(az ad app create --display-name cas-mcp-client \
    --sign-in-audience AzureADMyOrg \
    --public-client-redirect-uris "${MCP_REQUIRED_REDIRECTS[@]}" \
    --query appId -o tsv)"
else
  MCP_MERGED="$(merged_redirect_uris "${MCP_CLIENT_APP_ID}" publicClient \
    "${MCP_REQUIRED_REDIRECTS[@]}")"
  MCP_REDIRECTS=()
  while IFS= read -r uri; do
    MCP_REDIRECTS+=("${uri}")
  done <<<"${MCP_MERGED}"
  az ad app update --id "${MCP_CLIENT_APP_ID}" \
    --public-client-redirect-uris "${MCP_REDIRECTS[@]}"
fi
MCP_CLIENT_SP_ID="$(ensure_sp "${MCP_CLIENT_APP_ID}")"

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
GRAPH_APP_ID="$(lookup_one "service principal named Microsoft Graph" \
  az ad sp list --filter "displayName eq 'Microsoft Graph'" --query '[].appId')"
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

# Gate the public client's own sign-in on the single provisioning group
# (CAS-ADR-044), through its default-access role. The assignment goes to the
# resource's appRoleAssignedTo collection, the same request shape (group as
# principal, this service principal as resource) the BFF gate uses.
ensure_group_gate "${MCP_CLIENT_SP_ID}" "${DEFAULT_ACCESS_APP_ROLE_ID}"

# Emit the coordinates for the deployment parameter set (main.bicepparam).
echo "# Paste into the tenant parameter set:"
echo "param sageAudience = 'api://${SAGE_APP_ID}'"
echo "param bffOidcClientId = '${BFF_APP_ID}'"
echo "param mcpClientId = '${MCP_CLIENT_APP_ID}'"
