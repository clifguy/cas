#!/usr/bin/env bash
# Renew a GitHub Actions job's federated Azure sign-in from inside a step:
#
#   azure-federated-signin.sh [--if-stale]
#
# A federated sign-in stores the client assertion it was made with, and the
# Azure CLI re-presents that assertion whenever it mints a token -- for a scope
# it has not used yet, or to replace a cached token that has expired. Entra
# accepts the assertion for only a few minutes, so in a job that runs longer
# than that the first new mint fails (AADSTS700024). A workflow-level sign-in
# step cannot help inside one long-running step, which is where this runs.
#
# Each run requests a fresh token from the job's OIDC provider, signs in again
# with it, and records when it did. With --if-stale it does nothing while the
# recorded sign-in is younger than the renewal age below, so a polling loop can
# call it on every iteration and keep the stored assertion young enough to be
# accepted whenever the CLI next needs it.
#
# Outside GitHub Actions (no OIDC request URL in the environment) it does
# nothing, leaving an operator's own az session untouched. Inside, it requires
# the deploy identity's coordinates in AZURE_CLIENT_ID, AZURE_TENANT_ID and
# AZURE_SUBSCRIPTION_ID, and the job must hold the id-token: write permission.
set -euo pipefail

# Renew a sign-in older than this many seconds. The assertion is accepted for
# five minutes; renewing at four leaves a poll interval and one Azure CLI call
# of margin before a refresh would present an expired assertion.
RENEWAL_AGE=240

if_stale=false
case "$#:${1:-}" in
  0:) ;;
  1:--if-stale) if_stale=true ;;
  *)
    echo "usage: azure-federated-signin.sh [--if-stale]" >&2
    exit 2
    ;;
esac

if [ -z "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ]; then
  exit 0
fi

for name in ACTIONS_ID_TOKEN_REQUEST_TOKEN AZURE_CLIENT_ID AZURE_TENANT_ID \
  AZURE_SUBSCRIPTION_ID RUNNER_TEMP; do
  if [ -z "${!name:-}" ]; then
    echo "azure-federated-signin.sh: ${name} must be set inside GitHub Actions" >&2
    exit 1
  fi
done

stamp="${RUNNER_TEMP}/azure-federated-signin-at"
now="$(date +%s)"
if [ "${if_stale}" = true ] && [ -f "${stamp}" ]; then
  signed_in_at="$(cat "${stamp}")"
  case "${signed_in_at}" in
    '' | *[!0-9]*) ;;
    *)
      if [ $((now - signed_in_at)) -lt "${RENEWAL_AGE}" ]; then
        exit 0
      fi
      ;;
  esac
fi

# The request URL already carries a query string; the audience is the one
# Entra's workload identity federation exchanges.
response="$(curl --silent --show-error --fail \
  --header "Authorization: bearer ${ACTIONS_ID_TOKEN_REQUEST_TOKEN}" \
  "${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=api://AzureADTokenExchange")"
token="$(printf '%s' "${response}" \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["value"])')"
if [ -z "${token}" ]; then
  echo "azure-federated-signin.sh: the OIDC provider returned no token" >&2
  exit 1
fi
# Registers the token with the runner's log masking before anything could echo it.
echo "::add-mask::${token}"

az login --service-principal --username "${AZURE_CLIENT_ID}" \
  --tenant "${AZURE_TENANT_ID}" --federated-token "${token}" --output none
az account set --subscription "${AZURE_SUBSCRIPTION_ID}"
printf '%s\n' "${now}" >"${stamp}"
