#!/usr/bin/env bash
# Report the major version of the PostgreSQL Flexible Server in a resource
# group (PREFLIGHT_POSTGRES_SERVER_NAME selects the serving server during
# replacement; without it, exactly one server is required):
#
#   postgres-version-probe.sh <resource-group>
#
# Prints the major (e.g. `16`) on stdout and exits 0. Exits non-zero, printing
# nothing to stdout, when the selected server is absent/unreadable or an
# unqualified group lookup holds no Flexible Server or more than one:
# both are states where "the deployed major" has no single answer, and a probe
# that picked one anyway would report a version the deployment does not
# uniformly have.
#
# A control-plane read, not a database connection. The server integrates
# privately into a delegated subnet and is not reachable from a CI runner, but
# its declared version is a resource property, so the question can be asked from
# anywhere with an authenticated az session -- the deploy identity's federated
# login in CI. Requires no data-plane credential and opens no connection.
#
# Exists because repo-internal consistency is not the same claim as the server
# actually running the declared major: the template and every consumer can agree
# with each other while the live server sits elsewhere, which is exactly the
# state this probe is meant to report. Out-of-band drift has reached this
# surface before.
#
# Targets bash 3.2 (the stock macOS interpreter).
#
# Governed by CAS-ADR-042 (deployment profiles).
set -euo pipefail

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
  echo "usage: postgres-version-probe.sh <resource-group>" >&2
  exit 2
fi

if [ -n "${PREFLIGHT_POSTGRES_SERVER_NAME:-}" ]; then
  version="$(az postgres flexible-server show --resource-group "$1" \
    --name "$PREFLIGHT_POSTGRES_SERVER_NAME" --query version --output tsv)"
  case "$version" in
    ''|*[!0-9]*) echo "missing or invalid PostgreSQL major" >&2; exit 1 ;;
  esac
  printf '%s\n' "$version"
  exit 0
fi

versions="$(az postgres flexible-server list \
  --resource-group "$1" --query '[].version' --output tsv)"

count="$(printf '%s' "$versions" | grep -c '[0-9]' || true)"
if [ "$count" -eq 0 ]; then
  echo "no PostgreSQL Flexible Server found in resource group '$1'" >&2
  exit 1
fi
if [ "$count" -gt 1 ]; then
  echo "resource group '$1' holds $count Flexible Servers; the deployed major is ambiguous" >&2
  exit 1
fi

# The `version` property is the major on its own ("16"), not a full version
# string; trimmed rather than parsed so an unexpected shape reaches the caller
# intact instead of being silently reduced to its first digits.
printf '%s\n' "$versions" | tr -d '[:space:]'
