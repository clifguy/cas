#!/usr/bin/env bash
# Compute and emit the exact DNS records to publish for the cloud profile's two
# custom hostnames (CAS-ADR-042): the sage and cas CNAMEs plus the asuid
# domain-ownership TXT. This is the executable substance of
# docs/process/custom-domains-dns.md.
#
# DNS is provider-agnostic: the operator-controlled zone may live with any
# provider, so this script calls no DNS-provider API. It resolves the Azure
# coordinates from the deployment outputs and prints the records for the
# operator to publish by hand; the preflight verifies resolution separately.
set -euo pipefail

: "${DEPLOYMENT_NAME:?set DEPLOYMENT_NAME to the subscription deployment name}"

SAGE_HOST="$(az deployment sub show --name "${DEPLOYMENT_NAME}" \
  --query 'properties.outputs.sageCustomDomain.value' -o tsv)"
CAS_HOST="$(az deployment sub show --name "${DEPLOYMENT_NAME}" \
  --query 'properties.outputs.casCustomDomain.value' -o tsv)"
APIM_GATEWAY="$(az deployment sub show --name "${DEPLOYMENT_NAME}" \
  --query 'properties.outputs.apimGatewayUrl.value' -o tsv)"
APIM_HOST="${APIM_GATEWAY#https://}"

# The cas-side records depend on the BFF container app's ingress, which exists
# only once the app is deployed; resolve its FQDN and domain-ownership token.
: "${RG:?set RG to the resource group}"
: "${BFF_APP_NAME:?set BFF_APP_NAME to the CAS BFF container app name}"
BFF_FQDN="$(az containerapp show --name "${BFF_APP_NAME}" --resource-group "${RG}" \
  --query 'properties.configuration.ingress.fqdn' -o tsv)"
VERIFICATION_ID="$(az containerapp show --name "${BFF_APP_NAME}" --resource-group "${RG}" \
  --query 'properties.customDomainVerificationId' -o tsv)"

# Emit the records to publish in the tenant's own DNS provider (manual step).
echo "# Publish these records in the ${SAGE_HOST#sage.} zone:"
echo "# NAME                TYPE   VALUE"
echo "${SAGE_HOST}   CNAME   ${APIM_HOST}"
echo "${CAS_HOST}   CNAME   ${BFF_FQDN}"
echo "asuid.${CAS_HOST}   TXT   ${VERIFICATION_ID}"
