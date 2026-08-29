#!/usr/bin/env bash
# Ask the authorization server whether ONE OAuth resource identity is
# registered in the directory, by requesting a token scoped to it:
#
#   resource-token-probe.sh <resource-uri>
#
# Exits 0 iff a token scoped "<resource-uri>/.default" mints -- which Entra
# refuses (AADSTS500011, resource principal not found) exactly when the URI is
# not a registered identifier URI on any app in the tenant. The mint uses the
# v2 *scope* endpoint deliberately: the v1 --resource endpoint has a different
# issuer and would not exercise the same resolution a standards client's
# authorization request does.
#
# The probe's only signal is its exit status. The minted token is discarded
# (--output none) so it never reaches stdout, a CI log, or a caller's detail
# line; az's own diagnostics stay on stderr. Requires an authenticated az
# session (the deploy identity's federated login in CI) and no directory
# permission at all -- the refusal comes from the token endpoint, not from a
# Graph read. Targets bash 3.2 (the stock macOS interpreter).
#
# Governed by CAS-ADR-042 (deployment profiles).
set -euo pipefail

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
  echo "usage: resource-token-probe.sh <resource-uri>" >&2
  exit 2
fi

az account get-access-token --scope "$1/.default" --output none
