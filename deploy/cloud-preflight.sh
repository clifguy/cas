#!/usr/bin/env bash
#
# Post-deploy preflight probe for a CAS cloud tenant.
#
# Verifies every deployment layer of an already-deployed tenant INDEPENDENTLY
# and prints a single pass/fail matrix naming every failing layer. It reports;
# it never fixes (a verification artifact, not a deploy step). It is distinct
# from deploy/smoke.sh / bff-smoke.sh, which build+boot a container image
# locally -- this probe targets a live, remote environment over its public
# HTTPS endpoints plus an operator-supplied bearer token.
#
# Every check carries an ANTI-COINCIDENTAL CONTROL: a negative/expected-failure
# result (e.g. a 401) is credited only when a paired positive control (the OAuth
# discovery doc returning 200) proves the edge is genuinely live. A blanket edge
# failure (everything 404s) once made every probe "look as predicted"; the
# controls exist to catch exactly that.
#
# Usage:
#   deploy/cloud-preflight.sh            run the probe (reads the env below)
#   deploy/cloud-preflight.sh --dry-run  enumerate the check registry, no network
#
# Required environment:
#   AUTH_TOKEN                 Entra bearer token for the SAGE audience (obtained
#                              out of band, e.g. `az account get-access-token
#                              --scope <audience>/.default` -- the v2 endpoint, so
#                              the issuer matches APIM and the SAGE backend; this
#                              script never mints it)
#   SAGE_FQDN or BASE_DOMAIN   public SAGE host; SAGE_FQDN defaults to
#                              sage.<BASE_DOMAIN>, CAS_FQDN to cas.<BASE_DOMAIN>
#
# Optional environment (tenant parameters and operator/test seams):
#   CAS_FQDN                   public BFF host (default cas.<BASE_DOMAIN>)
#   SAGE_BASE_URL/CAS_BASE_URL override scheme+host (default https://<fqdn>)
#   PREFLIGHT_EXPECTED_VAULTS  comma-list of vault ids expected from discovery
#   PREFLIGHT_EXPECTED_ASUID   expected asuid TXT verification token
#   PREFLIGHT_VAULT_SOURCE     asserted vault_source_backend (e.g. document_store)
#   PREFLIGHT_CHECKS           comma-list allowlist of check ids to run
#   PREFLIGHT_SKIP             comma-list denylist of check ids to skip
#   PREFLIGHT_RESOLVE_CMD      DNS resolver invoked as `<cmd> <name> <type>`
#                              (default: dig +short)
#   PREFLIGHT_TLS_PROBE_CMD    TLS cert probe invoked as `<cmd> <host>`, prints
#                              the certificate SAN (default: openssl s_client)
#   PREFLIGHT_CURL_CMD         HTTP client (default: curl); seamed by the gate to
#                              drive probes offline
#   PREFLIGHT_SLEEP_CMD        warm-up retry delay command (default: sleep)
#   PREFLIGHT_WARMUP_MAX_ATTEMPTS   shared budget of connection-level (000) probe
#                              retries before the gate fails (default: 24)
#   PREFLIGHT_WARMUP_INTERVAL_SECONDS  seconds between warm-up retries
#                              (default: 5; with the default budget, ~120s total)
#   EXPECTED_SAGE_CNAME_SUFFIX expected sage CNAME target suffix (azure-api.net)
#   EXPECTED_CAS_CNAME_SUFFIX  expected cas CNAME target suffix
#                              (azurecontainerapps.io)
#
# Exit status: 0 iff no check FAILed (SKIP does not fail the run); non-zero
# otherwise. Targets bash 3.2 (the stock macOS interpreter) -- no associative
# arrays.
#
# Governed by CAS-ADR-042 (deployment profiles).
set -euo pipefail

# --------------------------------------------------------------------------- #
# Check registry (parallel indexed arrays -- bash 3.2 has no associative ones) #
# --------------------------------------------------------------------------- #
IDS=()
FNS=()
PREDS=()
CTRLS=()

register() { # id fn prediction control
  IDS+=("$1")
  FNS+=("$2")
  PREDS+=("$3")
  CTRLS+=("$4")
}

# Per-check verdicts are stored in dynamically-named `result_<id>` vars so a
# downstream qualifier can read an upstream verdict (bash 3.2, no maps).
set_result() { eval "result_$1=\"\$2\""; }
get_result() { eval "printf '%s' \"\${result_$1:-}\""; }

# --------------------------------------------------------------------------- #
# HTTP + resolver helpers (set globals; never echo to stdout)                 #
# --------------------------------------------------------------------------- #
HTTP_CODE=""
HTTP_BODY=""
VAULT_FIRST_ID=""
DETAIL_MSG=""

# A freshly-activated container revision can be briefly unroutable, surfacing as
# a connection-level curl failure (HTTP_CODE 000) even though the app is healthy
# -- the ingress has not yet placed the new revision in service. The probe
# tolerates that with a bounded warm-up retry keyed STRICTLY on 000: any real
# HTTP status (incl. 4xx/5xx) returns on the first attempt, so a genuine fault
# fails fast. The budget is shared across all probes and armed once, so a true
# outage exhausts it on the first check and every later 000 fails fast rather
# than multiplying the wait by the check count. See CAS-ADR-042.
WARMUP_BUDGET_REMAINING=""

# One probe attempt: sets HTTP_CODE (status, or 000 on a connection failure) and
# HTTP_BODY. The HTTP client is seamable (PREFLIGHT_CURL_CMD) so the gate can
# drive it offline; left at its default it is plain curl.
_curl_once() { # curl-arg...
  local code
  code="$($PREFLIGHT_CURL_CMD -sS -o "$WORKDIR/body" -w '%{http_code}' "$@" 2>/dev/null)" \
    || code=000
  HTTP_CODE="$code"
  HTTP_BODY="$(cat "$WORKDIR/body" 2>/dev/null || true)"
}

# Probe with the warm-up retry. Returns as soon as any real status arrives;
# retries only a connection-level 000, drawing on the shared budget.
_probe_with_warmup() { # curl-arg...
  _curl_once "$@"
  [ "$HTTP_CODE" != 000 ] && return 0
  [ -z "$WARMUP_BUDGET_REMAINING" ] && WARMUP_BUDGET_REMAINING="$PREFLIGHT_WARMUP_MAX_ATTEMPTS"
  while [ "$WARMUP_BUDGET_REMAINING" -gt 0 ]; do
    WARMUP_BUDGET_REMAINING=$((WARMUP_BUDGET_REMAINING - 1))
    $PREFLIGHT_SLEEP_CMD "$PREFLIGHT_WARMUP_INTERVAL_SECONDS"
    _curl_once "$@"
    [ "$HTTP_CODE" != 000 ] && return 0
  done
  return 0
}

http_get() { # url [bearer-token]
  local url="$1" auth="${2:-}"
  if [ -n "$auth" ]; then
    _probe_with_warmup -H "Authorization: Bearer $auth" "$url"
  else
    _probe_with_warmup "$url"
  fi
}

http_post() { # url json-body [bearer-token]
  local url="$1" data="$2" auth="${3:-}"
  if [ -n "$auth" ]; then
    _probe_with_warmup -H "Content-Type: application/json" \
      -H "Authorization: Bearer $auth" --data "$data" "$url"
  else
    _probe_with_warmup -H "Content-Type: application/json" --data "$data" "$url"
  fi
}

resolve_record() { # type name -> records, one per line
  local type="$1" name="$2"
  $PREFLIGHT_RESOLVE_CMD "$name" "$type" 2>/dev/null || true
}

# Default TLS probe: print the leaf certificate's subjectAltName. Overridable
# via PREFLIGHT_TLS_PROBE_CMD (the gate stubs it to exercise the control logic).
default_tls_probe() { # host
  local host="$1"
  openssl s_client -connect "${host}:443" -servername "$host" </dev/null 2>/dev/null \
    | openssl x509 -noout -ext subjectAltName 2>/dev/null
}

# --------------------------------------------------------------------------- #
# Checks -- each sets DETAIL_MSG (single line) and returns 0=PASS 1=FAIL 2=SKIP #
# --------------------------------------------------------------------------- #
edge_is_live() { # control: the OAuth discovery doc must answer 200 unauth
  http_get "$SAGE_BASE_URL/.well-known/oauth-protected-resource"
  [ "$HTTP_CODE" = 200 ]
}

check_edge_discovery() {
  http_get "$SAGE_BASE_URL/.well-known/oauth-protected-resource"
  if [ "$HTTP_CODE" = 200 ]; then
    DETAIL_MSG="discovery doc 200 unauth (edge policy live)"
    return 0
  fi
  DETAIL_MSG="expected 200 from the discovery doc, got $HTTP_CODE"
  return 1
}

check_edge_mcp_unauth() {
  http_get "$SAGE_BASE_URL/mcp"
  local mcp="$HTTP_CODE"
  if ! edge_is_live; then
    DETAIL_MSG="control failed: discovery doc not 200 (edge not live); /mcp $mcp is the blanket-failure trap, not auth-gating"
    return 1
  fi
  if [ "$mcp" = 401 ]; then
    DETAIL_MSG="/mcp 401 unauth (auth-gated) with the discovery-200 control held"
    return 0
  fi
  DETAIL_MSG="expected 401 from /mcp unauth, got $mcp (auth not enforced?)"
  return 1
}

check_edge_authn_backend() {
  http_get "$SAGE_BASE_URL/sage_vaults" "$AUTH_TOKEN"
  if [ "$HTTP_CODE" != 200 ]; then
    DETAIL_MSG="authenticated /sage_vaults returned $HTTP_CODE (expected 200)"
    return 1
  fi
  local body
  body="$(printf '%s' "$HTTP_BODY" | sed -e 's/^[[:space:]]*//')"
  case "$body" in
    \[*)
      DETAIL_MSG="authenticated request reached backend: 200 + JSON array (backend-shaped, not a canned edge 200)"
      return 0
      ;;
  esac
  DETAIL_MSG="200 but body is not a JSON array (possible canned edge response)"
  return 1
}

check_liveness() {
  http_get "$SAGE_BASE_URL/health"
  if [ "$HTTP_CODE" = 200 ] \
    && printf '%s' "$HTTP_BODY" | grep -q '"status"' \
    && printf '%s' "$HTTP_BODY" | grep -q 'ok'; then
    DETAIL_MSG="/health 200 status=ok (process up; store-free, not vault/store readiness)"
    return 0
  fi
  DETAIL_MSG="/health unhealthy: code=$HTTP_CODE"
  return 1
}

check_vault_load() {
  http_get "$SAGE_BASE_URL/sage_vaults" "$AUTH_TOKEN"
  if [ "$HTTP_CODE" != 200 ]; then
    DETAIL_MSG="expected 200 from /sage_vaults, got $HTTP_CODE"
    return 1
  fi
  local count missing v
  count="$(printf '%s' "$HTTP_BODY" | grep -o '"id"' | wc -l | tr -d ' ')"
  VAULT_FIRST_ID="$(printf '%s' "$HTTP_BODY" \
    | grep -oE '"id"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 \
    | sed -E 's/.*"([^"]*)"$/\1/')"
  if [ "$count" -lt 1 ]; then
    DETAIL_MSG="/sage_vaults 200 but zero vaults (startup likely aborted -- contrast with /health green)"
    return 1
  fi
  missing=""
  local IFS=','
  for v in $PREFLIGHT_EXPECTED_VAULTS; do
    [ -z "$v" ] && continue
    if ! printf '%s' "$HTTP_BODY" | grep -qE "\"id\"[[:space:]]*:[[:space:]]*\"$v\""; then
      missing="$missing $v"
    fi
  done
  if [ -n "$missing" ]; then
    DETAIL_MSG="$count vault(s) loaded but missing expected id(s):$missing"
    return 1
  fi
  DETAIL_MSG="$count vault(s) loaded; expected id(s) present"
  return 0
}

check_retrieval_pg() {
  local vid total ctrl
  vid="$VAULT_FIRST_ID"
  if [ -z "$vid" ]; then
    http_get "$SAGE_BASE_URL/sage_vaults" "$AUTH_TOKEN"
    vid="$(printf '%s' "$HTTP_BODY" \
      | grep -oE '"id"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 \
      | sed -E 's/.*"([^"]*)"$/\1/')"
  fi
  if [ -z "$vid" ]; then
    DETAIL_MSG="skipped: no vault id available to probe (vault_load produced none)"
    return 2
  fi
  http_post "$SAGE_BASE_URL/sage_vaults/$vid/discover" '{"mode":"catalog","limit":1}' "$AUTH_TOKEN"
  if [ "$HTTP_CODE" != 200 ]; then
    DETAIL_MSG="catalog discover on '$vid' returned $HTTP_CODE (Postgres path unhealthy)"
    return 1
  fi
  total="$(printf '%s' "$HTTP_BODY" \
    | grep -oE '"total_available"[[:space:]]*:[[:space:]]*[0-9]+' | grep -oE '[0-9]+$' | head -1)"
  if [ -z "$total" ]; then
    DETAIL_MSG="catalog discover 200 but no total_available field (not a DiscoverResponse)"
    return 1
  fi
  # Anti-coincidental control: a deterministic probe with no document_id must be
  # rejected 4xx by the app, proving requests are processed (not blanket-statused).
  http_post "$SAGE_BASE_URL/sage_vaults/$vid/discover" '{"mode":"deterministic"}' "$AUTH_TOKEN"
  ctrl="$HTTP_CODE"
  case "$ctrl" in
    4[0-9][0-9]) : ;;
    *)
      DETAIL_MSG="control failed: deterministic probe returned $ctrl, expected 4xx (app may not be processing requests)"
      return 1
      ;;
  esac
  DETAIL_MSG="catalog total_available=$total (real SQL ran); deterministic negative control $ctrl"
  return 0
}

check_kv_wildcard_tls() {
  local out esc
  out="$($PREFLIGHT_TLS_PROBE_CMD "$SAGE_FQDN" 2>/dev/null || true)"
  if [ -z "$out" ]; then
    DETAIL_MSG="TLS probe returned no certificate data for $SAGE_FQDN"
    return 1
  fi
  esc="$(printf '%s' "$BASE_DOMAIN" | sed 's/[.[\*^$/]/\\&/g')"
  if printf '%s' "$out" | grep -qE "\*\.$esc"; then
    DETAIL_MSG="leaf cert SAN covers *.$BASE_DOMAIN (Key Vault wildcard-tls resolved)"
    return 0
  fi
  DETAIL_MSG="handshake ok but cert SAN does not cover *.$BASE_DOMAIN (wrong/parked cert)"
  return 1
}

check_kv_anthropic() {
  if [ "$(get_result vault_load)" != PASS ]; then
    DETAIL_MSG="skipped: inferred from vault load; vault_load did not pass before this check"
    return 2
  fi
  DETAIL_MSG="inferred: vaults registered => the eager Key Vault anthropic-api-key fetch_secret succeeded at startup (not a direct KV read)"
  return 0
}

check_bff_liveness() {
  http_get "$CAS_BASE_URL/health"
  if [ "$HTTP_CODE" = 200 ] && printf '%s' "$HTTP_BODY" | grep -q '"status"'; then
    DETAIL_MSG="BFF /health 200 (process up; store-free)"
    return 0
  fi
  DETAIL_MSG="BFF /health unhealthy: code=$HTTP_CODE"
  return 1
}

check_bff_auth_configured() {
  http_get "$CAS_BASE_URL/app/auth/me"
  local me="$HTTP_CODE"
  if [ "$me" = 503 ]; then
    DETAIL_MSG="control: /app/auth/me 503 auth_not_configured (BFF OIDC config did not resolve)"
    return 1
  fi
  http_get "$CAS_BASE_URL/app/auth/login"
  if [ "$HTTP_CODE" != 200 ]; then
    DETAIL_MSG="/app/auth/login returned $HTTP_CODE (expected 200 with an authorization_url)"
    return 1
  fi
  if printf '%s' "$HTTP_BODY" | grep -q 'login.microsoftonline.com' \
    && printf '%s' "$HTTP_BODY" | grep -q 'client_id'; then
    DETAIL_MSG="OIDC front-channel configured (authorization_url -> Entra w/ client_id); /app/auth/me=$me. NB: bff-client-secret is back-channel/login-only, out of unattended scope"
    return 0
  fi
  DETAIL_MSG="/app/auth/login 200 but authorization_url is not a well-formed Entra URL"
  return 1
}

check_dns_cname() { # fqdn suffix
  local fqdn="$1" suffix="$2" got control
  got="$(resolve_record CNAME "$fqdn")"
  control="$(resolve_record CNAME "cas-preflight-nxdomain-control.$fqdn")"
  if [ -n "$control" ]; then
    DETAIL_MSG="control failed: bogus name resolved ('$control') -> resolver is wildcarding; '$fqdn' not creditable"
    return 1
  fi
  if printf '%s' "$got" | grep -qF "$suffix"; then
    DETAIL_MSG="$fqdn CNAME -> '$got' (matches '$suffix'); NXDOMAIN control held"
    return 0
  fi
  DETAIL_MSG="$fqdn CNAME '$got' does not match the expected target '$suffix'"
  return 1
}

check_dns_sage_cname() { check_dns_cname "$SAGE_FQDN" "$EXPECTED_SAGE_CNAME_SUFFIX"; }
check_dns_cas_cname() { check_dns_cname "$CAS_FQDN" "$EXPECTED_CAS_CNAME_SUFFIX"; }

check_dns_asuid_txt() {
  local name="asuid.$CAS_FQDN" got control
  got="$(resolve_record TXT "$name")"
  control="$(resolve_record TXT "asuid.cas-preflight-nxdomain-control.$CAS_FQDN")"
  if [ -n "$control" ]; then
    DETAIL_MSG="control failed: bogus asuid resolved -> resolver is wildcarding"
    return 1
  fi
  if [ -z "$got" ]; then
    DETAIL_MSG="no TXT at $name (domain-verification record absent)"
    return 1
  fi
  if [ -n "$PREFLIGHT_EXPECTED_ASUID" ]; then
    if printf '%s' "$got" | grep -qF "$PREFLIGHT_EXPECTED_ASUID"; then
      DETAIL_MSG="$name TXT matches the expected verification id"
      return 0
    fi
    DETAIL_MSG="$name TXT does not match the expected verification id"
    return 1
  fi
  DETAIL_MSG="$name TXT present; value unvalidated (PREFLIGHT_EXPECTED_ASUID not supplied)"
  return 0
}

check_sharepoint_discovery() {
  if [ "$(get_result vault_load)" != PASS ]; then
    DETAIL_MSG="skipped: depends on vault_load (SharePoint discovery is a startup-time fact observed via /sage_vaults)"
    return 2
  fi
  DETAIL_MSG="expected vaults present at last container start (asserted source: vault_source_backend=$PREFLIGHT_VAULT_SOURCE); startup-time fact via a request-time endpoint"
  return 0
}

# --------------------------------------------------------------------------- #
# Registry (order matters: vault_load precedes its downstream qualifiers)      #
# --------------------------------------------------------------------------- #
register edge_discovery check_edge_discovery \
  "OAuth discovery doc 200 unauthenticated" \
  "is itself the positive control crediting every other edge negative"
register edge_mcp_unauth check_edge_mcp_unauth \
  "/mcp 401 unauthenticated" \
  "credited only when the discovery doc returns 200 (edge live, not blanket-404)"
register edge_authn_backend check_edge_authn_backend \
  "authenticated /sage_vaults 200 with a JSON-array body" \
  "a backend-shaped array distinguishes a real backend from a canned edge 200"
register liveness check_liveness \
  "/health 200 status=ok" \
  "store-free endpoint; credited only as process-up, not store/vault readiness"
register vault_load check_vault_load \
  "/sage_vaults lists >=1 vault including the expected ids" \
  "contrast with /health-green: empty here means startup aborted"
register retrieval_pg check_retrieval_pg \
  "catalog discover 200 with total_available (a real SQL query ran)" \
  "a deterministic probe must be rejected 4xx, proving requests are processed not blanket-statused"
register kv_wildcard_tls check_kv_wildcard_tls \
  "leaf cert SAN covers the wildcard base domain" \
  "a wrong/parked cert subject fails even though the handshake succeeds"
register kv_anthropic check_kv_anthropic \
  "anthropic-api-key resolved (inferred from vault registration)" \
  "rides vault_load not /health; SKIP if vault_load did not pass"
register bff_liveness check_bff_liveness \
  "BFF /health 200 status=ok" \
  "store-free; credited only as BFF process-up"
register bff_auth_configured check_bff_auth_configured \
  "/app/auth/login 200 with an Entra authorization_url + client_id" \
  "credited only when /app/auth/me is not 503 (auth genuinely configured)"
register dns_sage_cname check_dns_sage_cname \
  "sage CNAME resolves to the expected gateway target" \
  "a bogus control name must NXDOMAIN, proving the resolver is not wildcarding"
register dns_cas_cname check_dns_cas_cname \
  "cas CNAME resolves to the expected app target" \
  "a bogus control name must NXDOMAIN, proving the resolver is not wildcarding"
register dns_asuid_txt check_dns_asuid_txt \
  "asuid TXT domain-verification record resolves" \
  "a bogus control name must NXDOMAIN; value matched against the operator-supplied token"
register sharepoint_discovery check_sharepoint_discovery \
  "expected vaults discovered from the SharePoint source at last start" \
  "a startup-time fact via a request-time endpoint; SKIP if vault_load did not pass"

# --------------------------------------------------------------------------- #
# Driver                                                                       #
# --------------------------------------------------------------------------- #
usage_and_exit() { # code [message...]
  local code="$1"
  shift || true
  if [ "$#" -gt 0 ]; then
    printf 'preflight: %s\n\n' "$*" >&2
  fi
  cat >&2 <<'EOF'
Usage: cloud-preflight.sh [--dry-run]

Post-deploy preflight probe for a CAS cloud tenant. Checks every layer
independently and prints a single pass/fail matrix; it reports, it never fixes.

Required environment:
  AUTH_TOKEN                 Entra bearer token for the SAGE audience
  SAGE_FQDN or BASE_DOMAIN   public SAGE host (sage.<BASE_DOMAIN>)

Optional: CAS_FQDN, SAGE_BASE_URL, CAS_BASE_URL, PREFLIGHT_EXPECTED_VAULTS,
PREFLIGHT_EXPECTED_ASUID, PREFLIGHT_VAULT_SOURCE, PREFLIGHT_CHECKS,
PREFLIGHT_SKIP, PREFLIGHT_RESOLVE_CMD, PREFLIGHT_TLS_PROBE_CMD. See the header
of this script for the full reference.
EOF
  exit "$code"
}

do_dry_run() {
  local i
  for i in "${!IDS[@]}"; do
    printf 'CHECK %s | prediction: %s | control: %s\n' "${IDS[$i]}" "${PREDS[$i]}" "${CTRLS[$i]}"
  done
  exit 0
}

in_csv() { # needle csv
  case ",$2," in
    *",$1,"*) return 0 ;;
    *) return 1 ;;
  esac
}

is_selected() { # id
  if [ -n "$PREFLIGHT_CHECKS" ] && ! in_csv "$1" "$PREFLIGHT_CHECKS"; then
    return 1
  fi
  if [ -n "$PREFLIGHT_SKIP" ] && in_csv "$1" "$PREFLIGHT_SKIP"; then
    return 1
  fi
  return 0
}

run_check() { # id fn
  local id="$1" fn="$2" rc status
  DETAIL_MSG="(no detail)"
  set +e
  "$fn"
  rc=$?
  set -e
  case "$rc" in
    0) status=PASS ;;
    2) status=SKIP ;;
    *) status=FAIL ;;
  esac
  set_result "$id" "$status"
  printf '%-4s %-22s %s\n' "$status" "$id" "$DETAIL_MSG"
  [ "$status" = FAIL ] && return 1
  return 0
}

# Decode and log the bearer token's iss/aud/ver claims (diagnostic only, never
# the token itself). The audience form a v2.0 access token carries -- the bare
# application-id GUID vs the api://<app-id> URI -- determines whether the APIM
# edge and the SAGE backend accept it; surfacing the observed claim shape makes
# an audience/issuer mismatch self-evident in the deploy log instead of inferred.
# Best-effort: a non-JWT token or a missing python3 degrades to "unavailable",
# never failing the preflight. The token is passed through the environment, not
# argv, so it never lands in the process table.
emit_token_claims() {
  if ! command -v python3 >/dev/null 2>&1; then
    echo "token-claims: unavailable (python3 not found)" >&2
    return 0
  fi
  local decoded
  decoded="$(_PF_DIAG_TOKEN="$AUTH_TOKEN" python3 -c '
import base64, json, os

token = os.environ.get("_PF_DIAG_TOKEN", "")
parts = token.split(".")
if len(parts) != 3:
    raise SystemExit(1)
segment = parts[1]
segment += "=" * (-len(segment) % 4)
claims = json.loads(base64.urlsafe_b64decode(segment))
print("iss=%s aud=%s ver=%s" % (claims.get("iss"), claims.get("aud"), claims.get("ver")))
' 2>/dev/null)" || true
  if [ -n "$decoded" ]; then
    echo "token-claims: $decoded" >&2
  else
    echo "token-claims: unavailable (not a decodable JWT)" >&2
  fi
}

main() {
  local fail=0 i id
  echo "=== CAS cloud preflight :: sage=$SAGE_FQDN cas=$CAS_FQDN ===" >&2
  emit_token_claims
  for i in "${!IDS[@]}"; do
    id="${IDS[$i]}"
    is_selected "$id" || continue
    if ! run_check "$id" "${FNS[$i]}"; then
      fail=1
    fi
  done
  echo "=== preflight complete (exit $fail) ===" >&2
  exit "$fail"
}

# --------------------------------------------------------------------------- #
# Entry                                                                        #
# --------------------------------------------------------------------------- #
DRY_RUN=0
case "${1:-}" in
  --dry-run) DRY_RUN=1 ;;
  -h | --help) usage_and_exit 0 ;;
  "") : ;;
  *) usage_and_exit 2 "unknown argument: $1" ;;
esac
[ "${PREFLIGHT_DRY_RUN:-0}" = 1 ] && DRY_RUN=1
if [ "$DRY_RUN" = 1 ]; then
  do_dry_run
fi

# Required-input validation (fail fast, before any network call).
[ -z "${AUTH_TOKEN:-}" ] && usage_and_exit 2 "AUTH_TOKEN is required"
if [ -z "${SAGE_FQDN:-}" ] && [ -z "${BASE_DOMAIN:-}" ]; then
  usage_and_exit 2 "SAGE_FQDN or BASE_DOMAIN is required"
fi

# Derive endpoints and apply tenant-parameter / seam defaults.
BASE_DOMAIN="${BASE_DOMAIN:-}"
SAGE_FQDN="${SAGE_FQDN:-sage.$BASE_DOMAIN}"
CAS_FQDN="${CAS_FQDN:-cas.$BASE_DOMAIN}"
[ -z "$BASE_DOMAIN" ] && BASE_DOMAIN="${SAGE_FQDN#*.}"
SAGE_BASE_URL="${SAGE_BASE_URL:-https://$SAGE_FQDN}"
CAS_BASE_URL="${CAS_BASE_URL:-https://$CAS_FQDN}"
PREFLIGHT_EXPECTED_VAULTS="${PREFLIGHT_EXPECTED_VAULTS:-}"
PREFLIGHT_EXPECTED_ASUID="${PREFLIGHT_EXPECTED_ASUID:-}"
PREFLIGHT_VAULT_SOURCE="${PREFLIGHT_VAULT_SOURCE:-unset}"
PREFLIGHT_CHECKS="${PREFLIGHT_CHECKS:-}"
PREFLIGHT_SKIP="${PREFLIGHT_SKIP:-}"
PREFLIGHT_RESOLVE_CMD="${PREFLIGHT_RESOLVE_CMD:-dig +short}"
PREFLIGHT_TLS_PROBE_CMD="${PREFLIGHT_TLS_PROBE_CMD:-default_tls_probe}"
PREFLIGHT_CURL_CMD="${PREFLIGHT_CURL_CMD:-curl}"
PREFLIGHT_SLEEP_CMD="${PREFLIGHT_SLEEP_CMD:-sleep}"
PREFLIGHT_WARMUP_MAX_ATTEMPTS="${PREFLIGHT_WARMUP_MAX_ATTEMPTS:-24}"
PREFLIGHT_WARMUP_INTERVAL_SECONDS="${PREFLIGHT_WARMUP_INTERVAL_SECONDS:-5}"
EXPECTED_SAGE_CNAME_SUFFIX="${EXPECTED_SAGE_CNAME_SUFFIX:-azure-api.net}"
EXPECTED_CAS_CNAME_SUFFIX="${EXPECTED_CAS_CNAME_SUFFIX:-azurecontainerapps.io}"

WORKDIR="$(mktemp -d 2>/dev/null || mktemp -d -t cas-preflight)"
trap 'rm -rf "$WORKDIR"' EXIT

main
