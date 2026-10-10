#!/usr/bin/env bash
# Make the deployment's Key Vault email the wildcard certificate's owner before
# the certificate expires (CAS-ADR-042).
#
# The certificate is administered outside this deployment, and its issuer warns
# its owner before expiry. That warning does not say this deployment still
# serves the certificate on both custom hostnames. A notice sent from the
# deployment's own vault does: it names the vault holding the certificate. This
# script adds the given addresses to the vault's certificate contacts and sets
# the certificate's lifetime action to email them a number of days before
# expiry. Re-running converges and changes nothing further.
#
# Certificate contacts are vault-wide, and the vault may hold other workloads'
# certificates, so contacts are only ever added, never removed.
#
# Inputs (environment):
#   CERT_EXPIRY_CONTACTS     comma-separated email addresses (required)
#   CERT_EXPIRY_NOTICE_DAYS  days before expiry to send the notice (default 30)
#   KEY_VAULT_NAME, or DEPLOYMENT_NAME to resolve it from the deployment outputs
#
# --validate-only checks the inputs and exits without calling Azure, so a
# caller can refuse bad input before making any write of its own.
#
# Run by the operator, who holds Key Vault Certificates Officer on the vault;
# the deploy identity cannot write certificate contacts or policy. See
# docs/process/key-vault-secrets.md.
set -euo pipefail

CERT_NAME="wildcard-tls"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

validate_only=0
case "${1:-}" in
  --validate-only) validate_only=1 ;;
  "") : ;;
  *) fail "unknown argument: $1 (the only option is --validate-only)" ;;
esac

contacts_raw="${CERT_EXPIRY_CONTACTS:-}"
days="${CERT_EXPIRY_NOTICE_DAYS:-30}"

case "$days" in
  '' | 0* | *[!0-9]*)
    fail "CERT_EXPIRY_NOTICE_DAYS must be a positive whole number of days, not '$days'"
    ;;
esac

case "$contacts_raw" in
  *[[:cntrl:]]*) fail "CERT_EXPIRY_CONTACTS carries a control character" ;;
esac

# Split the list, trim each entry, and refuse anything that is not plainly an
# address: one @ with text on both sides, no whitespace, and no leading '-',
# which the CLI would read as an option.
contacts=()
old_ifs="$IFS"
IFS=','
set -f
# shellcheck disable=SC2086 # the split on commas is the intent
set -- $contacts_raw
set +f
IFS="$old_ifs"
for entry in "$@"; do
  entry="$(printf '%s' "$entry" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  [ -z "$entry" ] && continue
  case "$entry" in
    -* | *[!A-Za-z0-9.@_%+\'-]* | *@*@* | @* | *@ | *@.* )
      fail "CERT_EXPIRY_CONTACTS entry '$entry' is not an email address"
      ;;
    *@*) contacts+=("$entry") ;;
    *) fail "CERT_EXPIRY_CONTACTS entry '$entry' is not an email address" ;;
  esac
done
[ "${#contacts[@]}" -gt 0 ] \
  || fail "set CERT_EXPIRY_CONTACTS to the certificate owner's email address(es), comma-separated"

[ "$validate_only" = 1 ] && exit 0

# The vault name is the keyVaultName deployment output. Supply it directly via
# KEY_VAULT_NAME, or set DEPLOYMENT_NAME to resolve it from the deployment.
KV="${KEY_VAULT_NAME:-}"
if [ -z "${KV}" ]; then
  : "${DEPLOYMENT_NAME:?set KEY_VAULT_NAME, or DEPLOYMENT_NAME to resolve it from deployment outputs}"
  KV="$(az deployment sub show --name "${DEPLOYMENT_NAME}" \
    --query 'properties.outputs.keyVaultName.value' -o tsv)"
fi

lower() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

umask 077
# An explicit template: without one, macOS mktemp ignores TMPDIR.
scratch="$(mktemp -d "${TMPDIR:-/tmp}/kv-notice.XXXXXX")"
trap 'rm -rf "${scratch}"' EXIT

# The current contacts, lower-cased, one per line. The listing is only a hint:
# the service answers a vault that has never held a contact with an error
# rather than an empty list, so an unavailable listing falls through to the
# add below, which handles that case itself and refuses an exact duplicate.
existing=""
if listed="$(az keyvault certificate contact list --vault-name "${KV}" \
  --query '[].email' -o tsv 2>/dev/null)"; then
  existing="$(lower "$listed")"
fi

for contact in "${contacts[@]}"; do
  if printf '%s\n' "$existing" | grep -qxF -- "$(lower "$contact")"; then
    echo "contact ${contact}: already present"
    continue
  fi
  if az keyvault certificate contact add --vault-name "${KV}" --email "${contact}" \
    --output none 2>"${scratch}/add-error"; then
    echo "contact ${contact}: added"
  elif grep -q "already exists" "${scratch}/add-error"; then
    echo "contact ${contact}: already present"
  else
    cat "${scratch}/add-error" >&2
    fail "could not add certificate contact ${contact} to ${KV}"
  fi
done

# The lifetime action. A partial policy updates only the field it names; the
# rest of the imported certificate's policy is left as it is.
printf '{"lifetimeActions":[{"trigger":{"daysBeforeExpiry":%s},"action":{"actionType":"EmailContacts"}}]}' \
  "$days" >"${scratch}/policy.json"
az keyvault certificate set-attributes --vault-name "${KV}" --name "${CERT_NAME}" \
  --policy "@${scratch}/policy.json" --output none

# Read the action back: an update the service accepted but did not apply must
# not report success.
applied="$(az keyvault certificate show --vault-name "${KV}" --name "${CERT_NAME}" \
  --query "policy.lifetimeActions[?action.actionType=='EmailContacts'].trigger.daysBeforeExpiry" \
  -o tsv)"
if ! printf '%s\n' "$applied" | grep -qx -- "$days"; then
  fail "${CERT_NAME} in ${KV} does not read back an EmailContacts action at ${days} days before expiry (read: '${applied}')"
fi
echo "${CERT_NAME} in ${KV}: contacts are emailed ${days} days before expiry"
