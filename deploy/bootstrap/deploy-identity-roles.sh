#!/usr/bin/env bash
# Grant the per-tenant deploy identity its Azure roles, narrowed to what the
# deployment needs. This is the executable substance of the role step in
# docs/process/azure-deployment.md.
#
# The orchestrator template deploys at subscription scope (it creates the
# tenant's resource group), so exactly one grant sits there: a custom role that
# can create, read, validate, preview and delete deployment records and nothing
# else. Resource Manager still authorizes every resource a deployment touches
# against the identity's other grants, so this role deploys nothing on its own.
# Everything else is held at the tenant's resource group:
#
#   Contributor                              the resources themselves
#   Role Based Access Control Administrator  the templates' own role assignments,
#                                            conditioned to the roles listed in
#                                            ASSIGNABLE_ROLES, granted to service
#                                            principals only
#   CAS deployment lock operator             the database server's delete lock,
#                                            which neither role above covers
#
# Modes:
#   (default)        register the resource providers the templates use, create
#                    the resource group when absent, create or verify the custom
#                    roles, and make the four assignments; idempotent.
#   --remove-legacy  remove every other role assignment the identity holds in the
#                    subscription (for example a subscription-scope Contributor and
#                    User Access Administrator pair, Owner, or AcrPush), refusing
#                    unless the narrowed set is in place. One inherited from a
#                    management group is reported and fails the run, but is left
#                    for removal at its own scope. Run it only after a deploy has
#                    succeeded on the narrowed set.
#   --show           print the identity's assignments and federated credentials,
#                    and fail on any listed assignment outside the narrowed set,
#                    including one inherited from above the subscription. The
#                    listing is eventually consistent: it is read again while
#                    part of the narrowed set is missing, but one that omits only
#                    an assignment outside the set cannot be told from a clean
#                    one, so a pass is evidence rather than proof.
#
# Every mode refuses while the application carries a federated credential that
# is not a plain GitHub environment subject from the GitHub issuer: the
# environment's protection rules are the gate on the deploy, and a branch,
# pull-request or claims-expression credential would let a token minted outside
# that gate sign in.
#
# Provider registration, resource-group creation and custom-role creation run
# under the operator's own rights; the deploy identity can do none of them. Pass
# in LOCATION the region the tenant's `location` deployment parameter names.
#
# Custom role names are unique per directory, so each carries the subscription
# id; tenants in different subscriptions of one directory get their own pair.
#
# Inputs: APP_ID (the deploy application's client id), SUBSCRIPTION_ID,
# RESOURCE_GROUP_NAME, and LOCATION for the default mode. Role definition ids are
# resolved by name at run time.
set -euo pipefail

: "${APP_ID:?set APP_ID to the client id of the deploy application}"
: "${SUBSCRIPTION_ID:?set SUBSCRIPTION_ID}"
: "${RESOURCE_GROUP_NAME:?set RESOURCE_GROUP_NAME to the resource group of the tenant}"

MODE="apply"
case "${1:-}" in
  "") ;;
  --remove-legacy) MODE="remove-legacy" ;;
  --show) MODE="show" ;;
  *)
    echo "usage: $0 [--remove-legacy | --show]" >&2
    exit 2
    ;;
esac

SUB_SCOPE="/subscriptions/${SUBSCRIPTION_ID}"
RG_SCOPE="${SUB_SCOPE}/resourceGroups/${RESOURCE_GROUP_NAME}"
GITHUB_ISSUER="https://token.actions.githubusercontent.com"

ORCHESTRATOR_ROLE="CAS deployment orchestrator (${SUBSCRIPTION_ID})"
LOCK_ROLE="CAS deployment lock operator (${SUBSCRIPTION_ID})"
RBAC_ADMIN_ROLE="Role Based Access Control Administrator"

# No cancel action: a run cannot stop another tenant's deployment.
ORCHESTRATOR_ACTIONS=(
  "Microsoft.Resources/deployments/delete"
  "Microsoft.Resources/deployments/operations/read"
  "Microsoft.Resources/deployments/operationstatuses/read"
  "Microsoft.Resources/deployments/read"
  "Microsoft.Resources/deployments/validate/action"
  "Microsoft.Resources/deployments/whatIf/action"
  "Microsoft.Resources/deployments/write"
)
LOCK_ACTIONS=(
  "Microsoft.Authorization/locks/delete"
  "Microsoft.Authorization/locks/read"
  "Microsoft.Authorization/locks/write"
)

# The built-in roles the templates assign. tests/deploy/test_deploy_identity_roles.py
# fails when a template assigns a role missing here.
ASSIGNABLE_ROLES=(
  "Monitoring Metrics Publisher"
  "AcrPull"
  "Key Vault Secrets User"
  "Key Vault Certificate User"
)

# The resource-provider namespaces the templates deploy. Registering one needs
# subscription-scope rights the deploy identity no longer holds, so a namespace
# a subscription has never used is registered here. The same test fails when a
# template starts using one missing from this list.
RESOURCE_PROVIDERS=(
  "Microsoft.ApiManagement"
  "Microsoft.App"
  "Microsoft.ContainerRegistry"
  "Microsoft.DBforPostgreSQL"
  "Microsoft.Insights"
  "Microsoft.KeyVault"
  "Microsoft.ManagedIdentity"
  "Microsoft.Network"
  "Microsoft.OperationalInsights"
)

check_credentials() {
  local rows subject issuer expression bad=0 count=0
  rows="$(az ad app federated-credential list --id "$APP_ID" \
    --query "[].join('|', [subject || '', issuer || '', claimsMatchingExpression.value || ''])" -o tsv)"
  while IFS='|' read -r subject issuer expression; do
    [ -n "$subject$issuer$expression" ] || continue
    count=$((count + 1))
    if [ -n "$expression" ]; then
      echo "ERROR: federated credential matches claims by expression: $expression" >&2
      bad=1
    elif [ "$issuer" != "$GITHUB_ISSUER" ]; then
      echo "ERROR: federated credential has issuer '$issuer', not $GITHUB_ISSUER" >&2
      bad=1
    else
      case "$subject" in
        repo:*/*:environment:*) echo "federated subject: $subject" ;;
        *)
          echo "ERROR: federated subject is not an environment subject: '$subject'" >&2
          bad=1
          ;;
      esac
    fi
  done <<EOF
$rows
EOF
  if [ "$count" -eq 0 ]; then
    echo "ERROR: the application has no federated credential" >&2
    bad=1
  fi
  return "$bad"
}

# Command substitution does not inherit errexit, so the helpers below that run
# inside one return their failures explicitly.
builtin_role_id() {
  local id
  id="$(az role definition list --name "$1" --query "[0].name" -o tsv)"
  if [ -z "$id" ]; then
    echo "ERROR: no role definition named '$1'" >&2
    return 1
  fi
  printf '%s\n' "$id"
}

# The role-definition and role-assignment listings are eventually consistent:
# a custom role or an assignment that exists can list as absent for a while,
# even after it has been seen. A lookup that comes back empty is retried,
# waiting between attempts, before what it looks for is taken as absent.
ROLE_LOOKUP_ATTEMPTS=5
ROLE_LOOKUP_DELAY=3

# Runs the listing command "$@", retrying an empty answer; prints nothing when
# every attempt is empty.
retry_listing() {
  local out attempt=1
  while :; do
    out="$("$@")" || return 1
    if [ -n "$out" ]; then
      printf '%s\n' "$out"
      return 0
    fi
    [ "$attempt" -lt "$ROLE_LOOKUP_ATTEMPTS" ] || return 0
    attempt=$((attempt + 1))
    sleep "$ROLE_LOOKUP_DELAY"
  done
}

# Lists the custom role named $1 with the query $2.
custom_role_query() {
  retry_listing az role definition list --custom-role-only true --name "$1" --scope "$SUB_SCOPE" \
    --query "$2" -o tsv
}

custom_role_id() {
  custom_role_query "$1" "[0].name"
}

ROLE_SHAPE_QUERY="[0].join('|', [to_string(length(permissions)), \
join(',', sort(permissions[0].actions)), join(',', permissions[0].notActions), \
join(',', permissions[0].dataActions), join(',', permissions[0].notDataActions), \
join(',', assignableScopes)])"

joined() {
  local IFS=","
  printf '%s' "$*"
}

ensure_custom_role() {
  local name="$1" description="$2" id shape expected json err
  shift 2
  id="$(custom_role_id "$name")" || return 1
  if [ -z "$id" ]; then
    json="$(printf '"%s",' "$@")"
    if ! err="$(az role definition create --role-definition "$(printf \
      '{"Name": "%s", "Description": "%s", "Actions": [%s], "NotActions": [], "AssignableScopes": ["%s"]}' \
      "$name" "$description" "${json%,}" "$SUB_SCOPE")" 2>&1 >/dev/null)"; then
      # A name collision means the role exists but had not yet been listed; it
      # is resolved and verified below like any other existing role.
      case "$err" in
        *RoleDefinitionWithSameNameExists*)
          echo "role '$name' already exists though it was not listed; resolving it" >&2
          ;;
        *)
          printf '%s\n' "$err" >&2
          return 1
          ;;
      esac
    fi
    id="$(custom_role_id "$name")" || return 1
    if [ -z "$id" ]; then
      echo "ERROR: role '$name' was not found after creation; re-run once it has propagated" >&2
      return 1
    fi
  fi
  # A role that exists is verified, not trusted: one widened by hand would
  # otherwise survive every re-run. The whole grant is compared -- the number of
  # permission blocks, the actions, any not-actions, any data actions and the
  # assignable scopes -- since widening can come through any of them.
  shape="$(custom_role_query "$name" "$ROLE_SHAPE_QUERY")" || return 1
  expected="1|$(joined "$@")||||$SUB_SCOPE"
  if [ "$shape" != "$expected" ]; then
    echo "ERROR: role '$name' is [$shape], expected [$expected]" \
      "(blocks|actions|notActions|dataActions|notDataActions|scopes); correct it or delete it and re-run" >&2
    return 1
  fi
  printf '%s\n' "$id"
}

# The identity's assignment of role $1 at scope $2 as id|condition, or nothing.
# The id makes the answer non-empty whenever the assignment is listed, so an
# unconditioned assignment is not mistaken for a missing one.
assignment_row() {
  retry_listing az role assignment list --assignee "$APP_ID" --scope "$2" --role "$1" \
    --query "[0].join('|', [id, condition || ''])" -o tsv
}

# Assigns role $1 at scope $2, conditioned by $3 when it is set. Returns 2 when
# the assignment already exists, 1 on any other failure.
create_assignment() {
  local err
  if [ -n "$3" ]; then
    err="$(az role assignment create --assignee "$APP_ID" --role "$1" --scope "$2" \
      --condition "$3" --condition-version "2.0" 2>&1 >/dev/null)" && return 0
  else
    err="$(az role assignment create --assignee "$APP_ID" --role "$1" --scope "$2" \
      2>&1 >/dev/null)" && return 0
  fi
  case "$err" in
    *RoleAssignmentExists*) return 2 ;;
  esac
  printf '%s\n' "$err" >&2
  return 1
}

ensure_assignment() {
  local role="$1" scope="$2" condition="${3:-}" row status=0
  row="$(assignment_row "$role" "$scope")" || return 1
  if [ -z "$row" ]; then
    create_assignment "$role" "$scope" "$condition" || status=$?
    case "$status" in
      0)
        echo "granted: $role at $scope"
        return 0
        ;;
      # The assignment exists but had not yet been listed; its condition is
      # read again and checked below like any other existing assignment's.
      2) echo "assignment of $role at $scope already exists though it was not listed" >&2 ;;
      *) return 1 ;;
    esac
    [ -n "$condition" ] || return 0
    row="$(assignment_row "$role" "$scope")" || return 1
  fi
  [ -n "$condition" ] || return 0
  [ "${row#*|}" != "$condition" ] || return 0
  # A condition cannot be edited in place by the CLI; replace the assignment.
  az role assignment delete --assignee "$APP_ID" --role "$role" --scope "$scope" || return 1
  status=0
  create_assignment "$role" "$scope" "$condition" || status=$?
  case "$status" in
    0) ;;
    2)
      echo "ERROR: the assignment of $role at $scope still exists after deleting it to" \
        "replace its condition; re-run once the role-assignment listing has caught up" >&2
      return 1
      ;;
    *) return 1 ;;
  esac
  echo "granted: $role at $scope"
}

assignment_condition() {
  local ids="" role id
  for role in "${ASSIGNABLE_ROLES[@]}"; do
    id="$(builtin_role_id "$role")" || return 1
    ids="${ids:+$ids, }$id"
  done
  local spn="ForAnyOfAnyValues:StringEqualsIgnoreCase {'ServicePrincipal'}"
  printf '%s' \
    "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR " \
    "(@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {$ids} " \
    "AND @Request[Microsoft.Authorization/roleAssignments:PrincipalType] $spn)) AND " \
    "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR " \
    "(@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {$ids} " \
    "AND @Resource[Microsoft.Authorization/roleAssignments:PrincipalType] $spn))"
}

# Every assignment the identity holds in the tenant's subscription, at or below
# it, plus those inherited from above it, one per line: id|role|scope|condition.
# Both reads name the subscription, so the operator's current CLI subscription
# cannot redirect them.
all_assignments() {
  local query="[].join('|', [id, roleDefinitionName || '', scope, condition || ''])"
  az role assignment list --assignee "$APP_ID" --all --subscription "$SUBSCRIPTION_ID" \
    --query "$query" -o tsv || return 1
  az role assignment list --assignee "$APP_ID" --scope "$SUB_SCOPE" --include-inherited \
    --query "$query" -o tsv || return 1
}

# Whether an assignment sits in the tenant's subscription, where this script
# may remove it, rather than being inherited from a management group above it.
in_subscription() {
  case "$1" in
    "$SUB_SCOPE" | "$SUB_SCOPE"/*) return 0 ;;
    *) return 1 ;;
  esac
}

NARROWED_SET=(
  "$ORCHESTRATOR_ROLE|$SUB_SCOPE"
  "Contributor|$RG_SCOPE"
  "$RBAC_ADMIN_ROLE|$RG_SCOPE"
  "$LOCK_ROLE|$RG_SCOPE"
)

# Whether one assignment row is part of the narrowed set.
is_narrowed() {
  local role="$1" scope="$2" condition="$3"
  case "$role|$scope" in
    "$ORCHESTRATOR_ROLE|$SUB_SCOPE" | "Contributor|$RG_SCOPE" | "$LOCK_ROLE|$RG_SCOPE") return 0 ;;
    "$RBAC_ADMIN_ROLE|$RG_SCOPE") [ "$condition" = "$EXPECTED_CONDITION" ] ;;
    *) return 1 ;;
  esac
}

# Prints the first role|scope of the narrowed set that the assignment rows in $1
# do not hold, or nothing when they hold all of it.
first_missing_narrowed() {
  local wanted found id role scope condition
  for wanted in "${NARROWED_SET[@]}"; do
    found=0
    while IFS='|' read -r id role scope condition; do
      if [ "$role|$scope" = "$wanted" ] && is_narrowed "$role" "$scope" "$condition"; then
        found=1
      fi
    done <<EOF
$1
EOF
    if [ "$found" -ne 1 ]; then
      printf '%s\n' "$wanted"
      return 0
    fi
  done
}

# The identity's assignment rows, deduplicated. A listing missing part of the
# narrowed set is read again, since it may be incomplete rather than accurate;
# the last read is returned either way.
assignment_rows() {
  local rows attempt=1
  while :; do
    rows="$(all_assignments)" || return 1
    rows="$(printf '%s\n' "$rows" | sort -u)"
    if [ -z "$(first_missing_narrowed "$rows")" ] || [ "$attempt" -ge "$ROLE_LOOKUP_ATTEMPTS" ]; then
      printf '%s\n' "$rows"
      return 0
    fi
    attempt=$((attempt + 1))
    sleep "$ROLE_LOOKUP_DELAY"
  done
}

check_credentials

case "$MODE" in
  show)
    EXPECTED_CONDITION="$(assignment_condition)"
    rows="$(assignment_rows)"
    extra=0
    while IFS='|' read -r id role scope condition; do
      [ -n "$id" ] || continue
      if is_narrowed "$role" "$scope" "$condition"; then
        echo "assignment: $role at $scope"
      else
        echo "OUTSIDE THE NARROWED SET: $role at $scope${condition:+ (conditioned)}" >&2
        extra=1
      fi
    done <<EOF
$rows
EOF
    exit "$extra"
    ;;

  apply)
    : "${LOCATION:?set LOCATION to the region of the tenant resource group}"
    for namespace in "${RESOURCE_PROVIDERS[@]}"; do
      az provider register --namespace "$namespace" --subscription "$SUBSCRIPTION_ID" --wait
    done
    if [ "$(az group exists --name "$RESOURCE_GROUP_NAME" --subscription "$SUBSCRIPTION_ID")" != "true" ]; then
      az group create --name "$RESOURCE_GROUP_NAME" --location "$LOCATION" \
        --subscription "$SUBSCRIPTION_ID" >/dev/null
      echo "created: resource group $RESOURCE_GROUP_NAME in $LOCATION"
    fi
    orchestrator_id="$(ensure_custom_role "$ORCHESTRATOR_ROLE" \
      "Create, read, validate and preview deployments; every deployed resource is authorized separately." \
      "${ORCHESTRATOR_ACTIONS[@]}")"
    lock_id="$(ensure_custom_role "$LOCK_ROLE" "Manage management locks." "${LOCK_ACTIONS[@]}")"
    condition="$(assignment_condition)"

    ensure_assignment "$orchestrator_id" "$SUB_SCOPE"
    ensure_assignment "Contributor" "$RG_SCOPE"
    ensure_assignment "$RBAC_ADMIN_ROLE" "$RG_SCOPE" "$condition"
    ensure_assignment "$lock_id" "$RG_SCOPE"
    ;;

  remove-legacy)
    EXPECTED_CONDITION="$(assignment_condition)"
    rows="$(assignment_rows)"
    missing="$(first_missing_narrowed "$rows")"
    if [ -n "$missing" ]; then
      echo "ERROR: the narrowed role set is not in place (missing: ${missing%%|*} at ${missing#*|}); run this script without flags first" >&2
      exit 1
    fi
    inherited=0
    while IFS='|' read -r id role scope condition; do
      [ -n "$id" ] || continue
      if is_narrowed "$role" "$scope" "$condition"; then
        continue
      fi
      if ! in_subscription "$scope"; then
        echo "NOT REMOVED (inherited from above the subscription): $role at $scope" >&2
        inherited=1
        continue
      fi
      az role assignment delete --ids "$id"
      echo "removed: $role at $scope"
    done <<EOF
$rows
EOF
    if [ "$inherited" -ne 0 ]; then
      echo "ERROR: remove the inherited assignments above at their own scope" >&2
      exit 1
    fi
    ;;
esac
