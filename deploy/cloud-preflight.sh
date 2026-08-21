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
# The interactive browser leg -- a human's OIDC sign-in and the on-behalf-of
# token exchange that follows it -- is inherently interactive and is NOT exercised
# here. This gate covers everything up to that login: the front-channel config
# (the BFF advertises a well-formed Entra authorization_url), BFF liveness, and
# the custom-domain TLS chain. The post-login user round-trip is confirmed once,
# by a manual browser sign-in, after a tenant's first bring-up.
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
#   PREFLIGHT_TLS_CHAIN_PROBE_CMD  TLS-chain probe invoked as `<cmd> <host>`,
#                              prints "<cert_count> <verify_code>" for the served
#                              chain (default: openssl s_client -showcerts)
#   PREFLIGHT_CURL_CMD         HTTP client (default: curl); seamed by the gate to
#                              drive probes offline
#   PREFLIGHT_MCP_PROBE_CMD    MCP round-trip probe invoked as
#                              `<cmd> --base-url <u> --mount <m> --mode <mode>`;
#                              reads the bearer from AUTH_TOKEN in the env, prints
#                              a one-line verdict, exits 0 on success (default:
#                              python3 <script-dir>/mcp_preflight_probe.py)
#   PREFLIGHT_SLEEP_CMD        warm-up retry delay command (default: sleep)
#   PREFLIGHT_WARMUP_MAX_ATTEMPTS   shared budget of connection-level (000) probe
#                              retries before the gate fails (default: 36)
#   PREFLIGHT_WARMUP_INTERVAL_SECONDS  seconds between warm-up retries
#                              (default: 5; with the default budget, ~180s total)
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

# Directory of this script, so it can locate sibling helpers (the MCP probe)
# regardless of the caller's working directory.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

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
HTTP_HEADERS=""
HTTP_DIAG=""
VAULT_FIRST_ID=""
DETAIL_MSG=""
MCP_PROBE_RC=""
MCP_PROBE_OUT=""

# A freshly-activated container revision can be briefly unroutable, surfacing as
# a connection-level curl failure (HTTP_CODE 000) even though the app is healthy
# -- the ingress has not yet placed the new revision in service. The probe
# tolerates that with a bounded warm-up retry keyed STRICTLY on 000: any real
# HTTP status (incl. 4xx/5xx) returns on the first attempt, so a genuine fault
# fails fast. The budget is shared across all probes and armed once, so a true
# outage exhausts it on the first check and every later 000 fails fast rather
# than multiplying the wait by the check count.
#
# A 000 is a connection-level failure that does not by itself distinguish "not
# yet routable" from "TLS handshake refused" from "DNS". So a terminal 000 is
# decoded from curl's exit code (HTTP_DIAG) and surfaced on the FAIL line, and a
# warm-up engagement leaves a one-line breadcrumb on stderr -- turning an opaque
# 000 into a diagnosis instead of a guess. See CAS-ADR-042.
WARMUP_BUDGET_REMAINING=""

# Decode a curl exit status into a human-readable connection-level failure
# reason. Each phrase embeds the literal `curl <code>` so the matrix line is an
# unambiguous, greppable signal: curl 7/28 ~ not-yet-routable/timeout (a wider
# warm-up budget can help), curl 35/51/60 ~ TLS/cert (a binding defect a budget
# cannot fix), curl 6 ~ DNS.
curl_exit_reason() { # curl-exit-code
  case "$1" in
    6) printf 'DNS resolution failed (curl 6)' ;;
    7) printf 'connection refused / not yet routable (curl 7)' ;;
    28) printf 'connection timed out (curl 28)' ;;
    35) printf 'TLS/SSL handshake failed (curl 35)' ;;
    51) printf 'TLS certificate verification failed (curl 51)' ;;
    56) printf 'network data receive failure (curl 56)' ;;
    60) printf 'TLS certificate not trusted (curl 60)' ;;
    *) printf 'connection-level failure (curl %s)' "$1" ;;
  esac
}

# One probe attempt: sets HTTP_CODE (status, or 000 on a connection failure),
# HTTP_BODY, HTTP_HEADERS (the raw response-header block, so a check can assert on
# a header such as Access-Control-Allow-Origin), and HTTP_DIAG (the decoded
# curl-exit reason on a 000, empty on a real status -- so a downstream FAIL line
# can name *why* a 000 occurred). curl's exit code is captured, not discarded. The
# HTTP client is seamable (PREFLIGHT_CURL_CMD) so the gate can drive it offline;
# left at its default it is plain curl.
_curl_once() { # curl-arg...
  local code rc
  if code="$($PREFLIGHT_CURL_CMD -sS -o "$WORKDIR/body" -D "$WORKDIR/headers" -w '%{http_code}' "$@" 2>/dev/null)"; then
    rc=0
  else
    rc=$?
  fi
  if [ "$rc" -ne 0 ]; then
    HTTP_CODE=000
    HTTP_DIAG="$(curl_exit_reason "$rc")"
  else
    HTTP_CODE="$code"
    HTTP_DIAG=""
  fi
  HTTP_BODY="$(cat "$WORKDIR/body" 2>/dev/null || true)"
  HTTP_HEADERS="$(cat "$WORKDIR/headers" 2>/dev/null || true)"
}

# Probe with the warm-up retry. Returns as soon as any real status arrives;
# retries only a connection-level 000, drawing on the shared budget.
_probe_with_warmup() { # curl-arg...
  _curl_once "$@"
  [ "$HTTP_CODE" != 000 ] && return 0
  if [ -z "$WARMUP_BUDGET_REMAINING" ]; then
    WARMUP_BUDGET_REMAINING="$PREFLIGHT_WARMUP_MAX_ATTEMPTS"
    # First 000 of the run: leave one breadcrumb naming the decoded reason and
    # the budget, so a human watching the live CI log understands the pause.
    if [ "$WARMUP_BUDGET_REMAINING" -gt 0 ]; then
      printf 'preflight: warm-up engaged (%s); retrying up to %s more time(s)...\n' \
        "$HTTP_DIAG" "$WARMUP_BUDGET_REMAINING" >&2
    fi
  fi
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

http_get_browser() { # url   (tokenless; a human-browser Accept header)
  _probe_with_warmup -H "Accept: text/html" "$1"
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

# A CORS preflight: an OPTIONS carrying the origin + request-method/-headers a
# browser sends ahead of the real cross-origin call. Sets HTTP_CODE and
# HTTP_HEADERS so a check can assert the preflight was answered with the
# Access-Control-Allow-* headers (the preflight-CORS fix). The Origin is a placeholder --
# the assertion is on the response headers, not the echoed origin.
http_options() { # url
  _probe_with_warmup -X OPTIONS \
    -H "Origin: https://cors-probe.invalid" \
    -H "Access-Control-Request-Method: POST" \
    -H "Access-Control-Request-Headers: authorization,content-type" \
    "$1"
}

# A transfer-endpoint probe: a deliberately-invalid one-time transfer token on
# the token-gated byte legs. The expected answer is the app's structured 410 --
# reaching it proves the dedicated APIM operation exists AND its policy skipped
# validate-jwt (a 401 means the edge intercepted the tokenless request instead).
http_put_transfer_probe() { # url token
  _probe_with_warmup -X PUT -H "X-Upload-Token: $2" --data "preflight-probe" "$1"
}

http_get_transfer_probe() { # url token
  _probe_with_warmup -H "X-Download-Token: $2" "$1"
}

_is_2xx() { # status-code
  [ "$1" -ge 200 ] && [ "$1" -lt 300 ]
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

# Default TLS-chain probe: print "<cert_count> <verify_code>" for the chain the
# host serves -- how many certificates are in the served chain, and openssl's
# verify return code. A complete, trusted chain is >=2 certs (leaf + issuing
# intermediate) with verify 0. Azure Container Apps serves a bound env
# certificate's PFX bytes verbatim, so a leaf-only PFX serves a 1-cert chain that
# strict clients reject (curl 60) even when the leaf SAN is valid -- a hole the
# SAN-only kv_wildcard_tls check cannot see. Overridable via
# PREFLIGHT_TLS_CHAIN_PROBE_CMD (the gate stubs it offline).
default_tls_chain_probe() { # host
  local host="$1" out count verify
  out="$(echo | openssl s_client -connect "${host}:443" -servername "$host" -showcerts 2>/dev/null)"
  count="$(printf '%s' "$out" | grep -c 'BEGIN CERTIFICATE' || true)"
  verify="$(printf '%s' "$out" | sed -n 's/.*Verify return code: \([0-9][0-9]*\).*/\1/p' | head -1)"
  [ -z "$verify" ] && verify=99
  printf '%s %s' "$count" "$verify"
}

# Run the MCP round-trip probe against a mount and capture its outcome into
# globals: MCP_PROBE_RC (exit code) and MCP_PROBE_OUT (its one-line verdict). The
# bearer token is read by the probe from AUTH_TOKEN in the environment, never
# argv, so it never lands in the process table. Seamable via
# PREFLIGHT_MCP_PROBE_CMD so the gate can stub it offline; left at its default it
# is the sibling Python helper (stdlib-only, so it runs on a bare CI runner that
# has no project virtualenv). Invoked only from within run_check, where errexit
# is already disabled, so a non-zero probe is captured rather than aborting.
mcp_probe() { # mount mode
  MCP_PROBE_OUT="$($PREFLIGHT_MCP_PROBE_CMD --base-url "$SAGE_BASE_URL" --mount "$1" --mode "$2" 2>/dev/null)"
  MCP_PROBE_RC=$?
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

check_edge_browser_redirect() {
  # A tokenless human browser (Accept: text/html) must be 302'd to the CAS app,
  # while a tokenless machine client (default Accept) still 401s. Probing both
  # makes the check discriminate Accept-routing from a blanket redirect; the
  # discovery-200 control guards against crediting either look on a dead edge.
  if ! edge_is_live; then
    http_get_browser "$SAGE_BASE_URL/mcp"
    DETAIL_MSG="control failed: discovery doc not 200 (edge not live); browser $HTTP_CODE is the blanket trap, not a redirect"
    return 1
  fi
  http_get_browser "$SAGE_BASE_URL/mcp"
  local browser="$HTTP_CODE"
  http_get "$SAGE_BASE_URL/mcp"
  local machine="$HTTP_CODE"
  if [ "$browser" = 302 ] && [ "$machine" = 401 ]; then
    DETAIL_MSG="browser (Accept: text/html) 302 to app; machine 401 held (Accept-routing, not blanket)"
    return 0
  fi
  if [ "$browser" != 302 ]; then
    DETAIL_MSG="expected 302 for Accept: text/html, got $browser (redirect not firing) with machine $machine"
    return 1
  fi
  DETAIL_MSG="browser 302 but machine got $machine not 401 (blanket redirect, not Accept-routing)"
  return 1
}

check_edge_cors_preflight() {
  # A browser-context MCP client sends a CORS preflight OPTIONS before the real
  # request; APIM must answer it anonymously (2xx + Access-Control-Allow-*) ahead
  # of validate-jwt, or the browser blocks the call. Probe the anonymous
  # /register and the authed /mcp -- both preflights route through the catch-all,
  # so both must carry CORS headers. The discovery-200 control guards against
  # crediting a preflight status on a dead edge.
  if ! edge_is_live; then
    DETAIL_MSG="control failed: discovery doc not 200 (edge not live); a preflight status is the blanket trap, not CORS handling"
    return 1
  fi
  http_options "$SAGE_BASE_URL/register"
  local reg_code="$HTTP_CODE" reg_hdrs="$HTTP_HEADERS"
  http_options "$SAGE_BASE_URL/mcp"
  local mcp_code="$HTTP_CODE" mcp_hdrs="$HTTP_HEADERS"
  local reg_cors=no mcp_cors=no
  printf '%s' "$reg_hdrs" | grep -qi 'access-control-allow-origin' && reg_cors=yes
  printf '%s' "$mcp_hdrs" | grep -qi 'access-control-allow-origin' && mcp_cors=yes
  if _is_2xx "$reg_code" && _is_2xx "$mcp_code" && [ "$reg_cors" = yes ] && [ "$mcp_cors" = yes ]; then
    DETAIL_MSG="OPTIONS /register $reg_code and /mcp $mcp_code carry Access-Control-Allow-* (preflight answered before the JWT gate)"
    return 0
  fi
  DETAIL_MSG="preflight not answered: /register $reg_code (cors=$reg_cors), /mcp $mcp_code (cors=$mcp_cors) -- validate-jwt still gating the preflight OPTIONS"
  return 1
}

check_edge_dcr_registration() {
  # A standards MCP client (Claude Desktop, the claude.ai connector) POSTs a DCR
  # request to /register and parses the response against its full
  # client-information schema before it ever reaches /authorize. Two fields in
  # that response gate the sign-in, and both slipped past the earlier gates:
  #   * redirect_uris -- a required, non-empty array in the client's parse; a
  #     response that omits it makes the client report "couldn't register" on a
  #     correct 201 with a valid client_id (the exact failure this check exists
  #     to catch).
  #   * scope -- must be resource-qualified (a "<resource>/Sage.Access" form);
  #     the bare "Sage.Access" leaves Entra unable to bind the scope to SAGE and
  #     it rejects /authorize (AADSTS650053). WHICH resource prefix is advertised
  #     is gated separately by edge_resource_identity.
  # The discovery-200 control guards against crediting a response shape on a dead
  # edge. This is a read-only probe: the facade registers nothing, so the POST has
  # no side effect.
  if ! edge_is_live; then
    DETAIL_MSG="control failed: discovery doc not 200 (edge not live); a /register body shape is the blanket trap, not real DCR handling"
    return 1
  fi
  http_post "$SAGE_BASE_URL/register" \
    '{"redirect_uris":["https://claude.ai/api/mcp/auth_callback"],"token_endpoint_auth_method":"none","grant_types":["authorization_code"],"response_types":["code"]}'
  local code="$HTTP_CODE" body="$HTTP_BODY"
  local ruris=no scope_ok=no
  printf '%s' "$body" | grep -qE '"redirect_uris"[[:space:]]*:[[:space:]]*\[[[:space:]]*"' && ruris=yes
  printf '%s' "$body" | grep -qE '"scope"[[:space:]]*:[[:space:]]*"[^"]*/Sage\.Access[[:space:]"]' && scope_ok=yes
  if _is_2xx "$code" && [ "$ruris" = yes ] && [ "$scope_ok" = yes ]; then
    DETAIL_MSG="/register $code returns non-empty redirect_uris and a resource-qualified scope (a standards MCP client can complete DCR)"
    return 0
  fi
  DETAIL_MSG="/register $code with redirect_uris=$ruris scope_qualified=$scope_ok -- a standards MCP client would fail its registration parse or bind the scope to the wrong resource"
  return 1
}

check_edge_resource_identity() {
  # The advertised OAuth identity must be the public SAGE host itself. Two
  # independent mechanisms break on a mismatch: (1) an MCP client rejects a
  # protected-resource-metadata `resource` that does not match the server origin
  # it connected to (RFC 9728 confused-deputy protection -- the reference client
  # throws "Protected resource ... does not match expected"); (2) the client
  # composes its RFC 8707 `resource` authorize parameter from these documents,
  # and Entra rejects the request pre-authentication when that parameter is not
  # consistent with the requested scope's resource (AADSTS9010010,
  # invalid_target). Advertising the internal *.azure-api.net gateway host, or
  # an api://-form scope prefix, therefore fails a standards client before the
  # login page ever renders -- and both reached prod because no gate compared
  # the advertised identity against the public host. The discovery-200 control
  # guards the blanket-edge trap.
  if ! edge_is_live; then
    DETAIL_MSG="control failed: discovery doc not 200 (edge not live); an advertised-identity match is the blanket trap, not identity coherence"
    return 1
  fi
  # Regex-escape the dots in the base URL so the ERE matches the host literally.
  local esc_base
  esc_base="$(printf '%s' "$SAGE_BASE_URL" | sed 's/[.]/\\./g')"
  http_get "$SAGE_BASE_URL/.well-known/oauth-protected-resource"
  local prm_body="$HTTP_BODY"
  local res_ok=no prm_scope_ok=no reg_scope_ok=no
  printf '%s' "$prm_body" \
    | grep -qE "\"resource\"[[:space:]]*:[[:space:]]*\"${esc_base}\"" && res_ok=yes
  printf '%s' "$prm_body" \
    | grep -qE "\"${esc_base}/Sage\.Access\"" && prm_scope_ok=yes
  http_post "$SAGE_BASE_URL/register" \
    '{"redirect_uris":["https://claude.ai/api/mcp/auth_callback"],"token_endpoint_auth_method":"none","grant_types":["authorization_code"],"response_types":["code"]}'
  printf '%s' "$HTTP_BODY" \
    | grep -qE "\"scope\"[[:space:]]*:[[:space:]]*\"${esc_base}/Sage\.Access[[:space:]\"]" && reg_scope_ok=yes
  if [ "$res_ok" = yes ] && [ "$prm_scope_ok" = yes ] && [ "$reg_scope_ok" = yes ]; then
    DETAIL_MSG="advertised resource, scopes_supported, and /register scope all carry $SAGE_BASE_URL (identity coherent with the public host)"
    return 0
  fi
  DETAIL_MSG="advertised identity is not the public host: resource=$res_ok prm_scope=$prm_scope_ok register_scope=$reg_scope_ok -- a standards MCP client fails RFC 9728 origin validation or Entra rejects its resource parameter (AADSTS9010010)"
  return 1
}

check_edge_advertises_offline_access() {
  # The public MCP client requests the bare OIDC offline_access scope so Entra
  # issues it a refresh token; without it a v2 access token (60-90 min) expires
  # with no silent renewal, forcing a fresh /authorize round trip every hour or
  # two (CAS-ADR-042). The edge must advertise offline_access in BOTH the
  # protected-resource-metadata scopes_supported AND the /register scope string,
  # or a standards client never asks for it. WHICH scope prefix carries the
  # resource identity is gated by edge_resource_identity; this asserts the
  # separate offline_access token specifically, so a regression that keeps
  # Sage.Access intact while dropping offline_access is caught here rather than
  # sliding through green. The discovery-200 control guards the blanket-edge trap.
  if ! edge_is_live; then
    DETAIL_MSG="control failed: discovery doc not 200 (edge not live); an offline_access advertisement is the blanket trap, not a real refresh-token scope"
    return 1
  fi
  http_get "$SAGE_BASE_URL/.well-known/oauth-protected-resource"
  local prm_ok=no reg_ok=no
  printf '%s' "$HTTP_BODY" | grep -qE '"offline_access"' && prm_ok=yes
  http_post "$SAGE_BASE_URL/register" \
    '{"redirect_uris":["https://claude.ai/api/mcp/auth_callback"],"token_endpoint_auth_method":"none","grant_types":["authorization_code"],"response_types":["code"]}'
  printf '%s' "$HTTP_BODY" \
    | grep -qE '"scope"[[:space:]]*:[[:space:]]*"[^"]*offline_access' && reg_ok=yes
  if [ "$prm_ok" = yes ] && [ "$reg_ok" = yes ]; then
    DETAIL_MSG="scopes_supported and the /register scope both advertise offline_access (a standards MCP client requests a refresh token)"
    return 0
  fi
  DETAIL_MSG="offline_access advertisement missing: scopes_supported=$prm_ok register_scope=$reg_ok -- a standards MCP client won't request offline_access and its access token expires with no silent renewal"
  return 1
}

check_edge_advertises_grant_types() {
  # A conformant OAuth client reads the authorization-server metadata before it
  # acts and never attempts a grant the resource does not advertise, so an
  # unadvertised grant is indistinguishable from an unsupported one. The edge
  # honors two: authorization_code for the interactive browser flow, and
  # client_credentials for machine callers bearing the Sage.Reader app role
  # (CAS-ADR-042). Each is asserted on its own rather than as one match over the
  # whole array, so dropping either -- while the document keeps 200ing and every
  # other edge check stays green -- is caught here instead of sliding through.
  # The discovery-200 control guards the blanket-edge trap.
  if ! edge_is_live; then
    DETAIL_MSG="control failed: discovery doc not 200 (edge not live); an advertised grant set is the blanket trap, not a real grant advertisement"
    return 1
  fi
  http_get "$SAGE_BASE_URL/.well-known/oauth-authorization-server"
  local as_code="$HTTP_CODE" grants ac_ok=no cc_ok=no
  # Scope the match to the grant_types_supported array so a grant name appearing
  # in another field, or in a neighbouring array, cannot credit this check.
  grants="$(printf '%s' "$HTTP_BODY" \
    | sed -n 's/.*"grant_types_supported"[[:space:]]*:[[:space:]]*\[\([^]]*\)\].*/\1/p')"
  printf '%s' "$grants" | grep -qE '"authorization_code"' && ac_ok=yes
  printf '%s' "$grants" | grep -qE '"client_credentials"' && cc_ok=yes
  if [ "$as_code" = 200 ] && [ "$ac_ok" = yes ] && [ "$cc_ok" = yes ]; then
    DETAIL_MSG="grant_types_supported advertises authorization_code and client_credentials (a conformant client can reach the machine-to-machine leg, not just the interactive one)"
    return 0
  fi
  DETAIL_MSG="advertised grant set incomplete: as_metadata=$as_code authorization_code=$ac_ok client_credentials=$cc_ok -- a conformant client will not attempt an unadvertised grant, so the missing leg is unreachable even though the edge honors it"
  return 1
}

check_edge_serves_openapi_spec() {
  # The OpenAPI schema document must be fetchable WITHOUT a token, so a
  # developer outside the repository can generate a working client against a
  # deployment they only know the domain of. Gating it is self-defeating: the
  # document naming the token endpoint sat behind that same token, leaving a
  # first-time caller no entry point at all. Two independent legs have to be
  # open (SAGE's own auth exemption and a dedicated APIM operation whose policy
  # omits the base include), and either one regressing lands here as a 401.
  #
  # A 200 alone is not enough, so two further conditions run. The body must be
  # SAGE's own document declaring the bearer scheme: an image predating the
  # declaration serves a well-formed document that never mentions
  # authentication, which leaves the caller as stuck as a 401 would, so the
  # grep misses and this FAILs. And a still-gated surface must still answer
  # 401: publishing one document must not have widened into an open edge, which
  # would make the document's own 200 mean nothing. The discovery-200 control
  # guards the blanket-edge trap.
  if ! edge_is_live; then
    DETAIL_MSG="control failed: discovery doc not 200 (edge not live); a schema document 200 is the blanket trap, not a real exemption"
    return 1
  fi
  http_get "$SAGE_BASE_URL/openapi.json"
  local spec_code="$HTTP_CODE" spec_body="$HTTP_BODY"
  if [ "$spec_code" != 200 ]; then
    DETAIL_MSG="/openapi.json returned $spec_code unauthenticated; a caller still needs a token to read the document that says how to obtain one (check the SAGE auth exemption and the APIM operation policy)"
    return 1
  fi
  if ! printf '%s' "$spec_body" | grep -qE '"title"[[:space:]]*:[[:space:]]*"SAGE Core API"'; then
    DETAIL_MSG="/openapi.json 200 but the body is not the SAGE Core API document (some other surface answered at that path)"
    return 1
  fi
  if ! printf '%s' "$spec_body" | grep -qE '"entraBearer"'; then
    DETAIL_MSG="/openapi.json 200 but declares no entraBearer security scheme; the document never states how to authenticate (image predates the declaration)"
    return 1
  fi
  http_get "$SAGE_BASE_URL/mcp"
  if [ "$HTTP_CODE" != 401 ]; then
    DETAIL_MSG="control failed: /mcp answered $HTTP_CODE unauthenticated, expected 401; the edge gate has collapsed, so the schema document's 200 is not a deliberate exemption"
    return 1
  fi
  DETAIL_MSG="/openapi.json 200 unauth, is the SAGE document, declares entraBearer; /mcp still 401 (the exemption is one path, not an open edge)"
  return 0
}

check_edge_mount_discovery() {
  # Each MCP mount must steer a denied client to its RFC 9728 PATH-INSERTED
  # metadata document, and that document must advertise the PATH-CARRYING mount
  # URI as its resource. A client normalizes the advertised resource through a
  # URL serializer and sends the result as its RFC 8707 resource /authorize
  # parameter; a bare-origin resource serializes WITH a trailing slash
  # (https://host -> https://host/), a form Entra can neither match against a
  # registered identifier URI (byte-for-byte; AADSTS9010010) nor register as
  # one (invalid alias) -- so a client steered to a bare-host resource
  # dead-ends at /authorize with no registrable fix. Verified live on cor-prod.
  # Two legs per mount: the 401 challenge points at the mount document (not the
  # root), and the mount document 200s with the mount URI as resource. The
  # discovery-200 control guards the blanket-edge trap.
  if ! edge_is_live; then
    DETAIL_MSG="control failed: discovery doc not 200 (edge not live); a challenge/document shape is the blanket trap, not mount discovery"
    return 1
  fi
  local esc_base
  esc_base="$(printf '%s' "$SAGE_BASE_URL" | sed 's/[.]/\\./g')"
  local detail="" ok=yes
  local mount
  for mount in /mcp /mcp_admin; do
    http_get "$SAGE_BASE_URL$mount"
    local challenge_ok=no
    # The closing quote anchors exactness: the /mcp challenge must point at
    # .../oauth-protected-resource/mcp", never the root doc or another mount's.
    printf '%s' "$HTTP_HEADERS" \
      | grep -qE "resource_metadata=\"${esc_base}/\.well-known/oauth-protected-resource${mount}\"" && challenge_ok=yes
    http_get "$SAGE_BASE_URL/.well-known/oauth-protected-resource$mount"
    local doc_code="$HTTP_CODE" doc_res_ok=no
    printf '%s' "$HTTP_BODY" \
      | grep -qE "\"resource\"[[:space:]]*:[[:space:]]*\"${esc_base}${mount}\"" && doc_res_ok=yes
    [ "$challenge_ok" = yes ] && [ "$doc_code" = 200 ] && [ "$doc_res_ok" = yes ] || ok=no
    detail="$detail $mount(challenge=$challenge_ok doc=$doc_code resource=$doc_res_ok)"
  done
  if [ "$ok" = yes ]; then
    DETAIL_MSG="each mount's 401 challenge points at its path-inserted metadata and each document advertises the path-carrying mount URI:$detail"
    return 0
  fi
  DETAIL_MSG="mount discovery incoherent:$detail -- a client steered to a bare-host resource serializes a trailing-slash form Entra can never match (AADSTS9010010)"
  return 1
}

check_mcp_admin() {
  http_get "$SAGE_BASE_URL/mcp_admin"
  local unauth="$HTTP_CODE"
  if ! edge_is_live; then
    DETAIL_MSG="control failed: discovery doc not 200 (edge not live); /mcp_admin $unauth is the blanket-failure trap, not auth-gating"
    return 1
  fi
  # The unauth-401 gate is itself the control that credits the authed handshake:
  # a blanket-200 surface would fail here (200 != 401), so a passing handshake
  # below is genuinely auth-gated, not a canned 200. Per CAS-ADR-034 the
  # maintenance mount is auth-gated, not authz-role-gated -- no admin role is
  # asserted; the same machine token works on both mounts.
  if [ "$unauth" != 401 ]; then
    DETAIL_MSG="expected 401 from /mcp_admin unauth, got $unauth (maintenance mount auth not enforced?)"
    return 1
  fi
  mcp_probe /mcp_admin handshake
  if [ "$MCP_PROBE_RC" != 0 ]; then
    DETAIL_MSG="/mcp_admin 401 unauth held but the authenticated maintenance handshake failed [${MCP_PROBE_OUT:-no verdict}]"
    return 1
  fi
  DETAIL_MSG="/mcp_admin 401 unauth + authenticated handshake ok [$MCP_PROBE_OUT]; discovery-200 control held"
  return 0
}

check_mcp_roundtrip() {
  if ! edge_is_live; then
    DETAIL_MSG="control failed: discovery doc not 200 (edge not live); an MCP round-trip result would be the blanket-failure trap"
    return 1
  fi
  mcp_probe /mcp roundtrip
  if [ "$MCP_PROBE_RC" != 0 ]; then
    DETAIL_MSG="/mcp round-trip failed [${MCP_PROBE_OUT:-no verdict}]"
    return 1
  fi
  DETAIL_MSG="/mcp initialize + tools/list ok, unknown method -> JSON-RPC error [$MCP_PROBE_OUT]; discovery-200 control held"
  return 0
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

check_ocr_capability() {
  # The running SAGE container probes its OCR toolchain at startup (ocrmypdf
  # importable + tesseract/ghostscript on PATH) and advertises the result on the
  # store-free /health envelope, so this out-of-container harness can confirm the
  # deployed image actually ships OCR. All three must report true. A stale image
  # predating the ocr field returns a healthy 200 *without* it -- the greps then
  # miss and the check FAILs. That is the anti-coincidental control: the field's
  # presence (and per-binary truth) is the container's real probe, not a blanket
  # 200. This is the gate that would have caught the toolchain-less image.
  http_get "$SAGE_BASE_URL/health"
  if [ "$HTTP_CODE" != 200 ]; then
    DETAIL_MSG="/health not 200 (code=$HTTP_CODE); cannot read OCR capability"
    return 1
  fi
  local missing=""
  printf '%s' "$HTTP_BODY" | grep -qE '"ocrmypdf"[[:space:]]*:[[:space:]]*true' \
    || missing="$missing ocrmypdf"
  printf '%s' "$HTTP_BODY" | grep -qE '"tesseract"[[:space:]]*:[[:space:]]*true' \
    || missing="$missing tesseract"
  printf '%s' "$HTTP_BODY" | grep -qE '"ghostscript"[[:space:]]*:[[:space:]]*true' \
    || missing="$missing ghostscript"
  if [ -n "$missing" ]; then
    DETAIL_MSG="/health OCR capability missing or false:$missing (image lacks the OCR toolchain or predates the ocr field)"
    return 1
  fi
  DETAIL_MSG="/health advertises OCR toolchain: ocrmypdf+tesseract+ghostscript all true"
  return 0
}

check_transfer_upload_gate() {
  # PUT /upload with a garbage token must come back as the app's structured
  # 410 transfer_token_invalid: that single status proves the dedicated APIM
  # operation routes, its policy skipped validate-jwt, and the SAGE app's
  # token check answered. A 401 means the edge intercepted the tokenless
  # request (operation missing or its policy inherited <base/>); a 404 means
  # the operation or the app route is absent.
  if ! edge_is_live; then
    DETAIL_MSG="control failed: discovery doc not 200 (edge not live); upload probe is the blanket-failure trap"
    return 1
  fi
  http_put_transfer_probe "$SAGE_BASE_URL/upload" "preflight-probe.invalid"
  if [ "$HTTP_CODE" = 410 ] && printf '%s' "$HTTP_BODY" | grep -q 'transfer_token_invalid'; then
    DETAIL_MSG="/upload 410 transfer_token_invalid unauth (operation live, jwt skipped, app token gate answered)"
    return 0
  fi
  if [ "$HTTP_CODE" = 401 ]; then
    DETAIL_MSG="/upload 401: validate-jwt intercepted the tokenless byte leg (operation missing or policy inherits <base/>)"
    return 1
  fi
  DETAIL_MSG="expected 410 transfer_token_invalid from /upload, got $HTTP_CODE"
  return 1
}

check_transfer_download_gate() {
  # GET /download/{id} with a garbage token: same three-way discrimination as
  # the upload probe, on the templated operation (an undeclared template
  # parameter surfaces here as the gateway's generic 404).
  if ! edge_is_live; then
    DETAIL_MSG="control failed: discovery doc not 200 (edge not live); download probe is the blanket-failure trap"
    return 1
  fi
  http_get_transfer_probe "$SAGE_BASE_URL/download/preflightprobe" "preflight-probe.invalid"
  if [ "$HTTP_CODE" = 410 ] && printf '%s' "$HTTP_BODY" | grep -q 'transfer_token_invalid'; then
    DETAIL_MSG="/download/{id} 410 transfer_token_invalid unauth (operation live, jwt skipped, app token gate answered)"
    return 0
  fi
  if [ "$HTTP_CODE" = 401 ]; then
    DETAIL_MSG="/download/{id} 401: validate-jwt intercepted the tokenless byte leg (operation missing or policy inherits <base/>)"
    return 1
  fi
  DETAIL_MSG="expected 410 transfer_token_invalid from /download/{id}, got $HTTP_CODE"
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

check_bff_custom_domain_tls() {
  local out count verify
  out="$($PREFLIGHT_TLS_CHAIN_PROBE_CMD "$CAS_FQDN" 2>/dev/null || true)"
  count="${out%% *}"
  verify="${out##* }"
  case "$count" in
    '' | *[!0-9]*)
      DETAIL_MSG="TLS chain probe returned no usable result for $CAS_FQDN (handshake failed?)"
      return 1
      ;;
  esac
  if [ "$count" -lt 1 ]; then
    DETAIL_MSG="no certificate served by $CAS_FQDN (TLS handshake produced no chain)"
    return 1
  fi
  if [ "$count" -lt 2 ]; then
    DETAIL_MSG="incomplete chain: $CAS_FQDN served $count cert (leaf only; issuing intermediate missing) -- strict clients reject it (curl 60) though the leaf SAN is valid; ACA serves the env-cert PFX verbatim"
    return 1
  fi
  if [ "$verify" != 0 ]; then
    DETAIL_MSG="$CAS_FQDN served $count certs but the chain does not verify as trusted (openssl code $verify)"
    return 1
  fi
  DETAIL_MSG="$CAS_FQDN serves a complete, trusted chain ($count certs; verify 0)"
  return 0
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
register edge_browser_redirect check_edge_browser_redirect \
  "a browser (Accept: text/html) is 302-redirected to the CAS app" \
  "the machine Accept must still 401 with the discovery-200 control held, proving Accept-routing not a blanket redirect"
register edge_cors_preflight check_edge_cors_preflight \
  "OPTIONS /register and /mcp answer 2xx with Access-Control-Allow-* (preflight answered before the JWT gate)" \
  "a 2xx without the CORS header, or the preflight-specific 401, is the coincidental-pass trap; credited only with the discovery-200 control held"
register edge_dcr_registration check_edge_dcr_registration \
  "POST /register returns a non-empty redirect_uris array and a resource-qualified scope (a standards MCP client completes DCR then binds the scope to SAGE)" \
  "a 2xx whose body omits redirect_uris or advertises the bare 'Sage.Access' is the coincidental-pass trap; credited only with the discovery-200 control held"
register edge_resource_identity check_edge_resource_identity \
  "the advertised resource, scopes_supported, and /register scope all carry the public SAGE base URL (RFC 9728 origin match; Entra accepts the client's RFC 8707 resource parameter)" \
  "an internal-gateway resource or api://-form scope prefix is the coincidental-pass trap (both 200 fine at the edge yet fail a standards client); credited only with the discovery-200 control held"
register edge_advertises_offline_access check_edge_advertises_offline_access \
  "scopes_supported and the /register scope both advertise offline_access (the public MCP client requests a refresh token so its session survives past the access-token lifetime)" \
  "offline_access dropped from either leg on a genuinely-live edge is the regression trap (Sage.Access stays intact so every other edge check is green); credited only with the discovery-200 control held"
register edge_advertises_grant_types check_edge_advertises_grant_types \
  "the authorization-server metadata advertises both authorization_code and client_credentials (a conformant client can reach the machine-to-machine leg, not just the interactive one)" \
  "either grant dropped from a genuinely-live edge is the regression trap (the document keeps 200ing and every other edge check stays green); credited only with the discovery-200 control held"
register edge_serves_openapi_spec check_edge_serves_openapi_spec \
  "GET /openapi.json returns 200 unauthenticated carrying SAGE's own document with its entraBearer scheme (an outside developer can generate a working client from the deployment alone)" \
  "a 200 that is some other document, or one declaring no security scheme, is the coincidental-pass trap; /mcp must still 401 so an open edge cannot credit it; discovery-200 credits the fetch"
register edge_mount_discovery check_edge_mount_discovery \
  "each MCP mount's 401 challenge points at its path-inserted metadata document, whose resource is the path-carrying mount URI" \
  "a bare-host resource is the coincidental-pass trap (200s fine, but serializes to a trailing-slash form Entra can neither match nor register -- AADSTS9010010); credited only with the discovery-200 control held"
register mcp_admin check_mcp_admin \
  "/mcp_admin 401 unauthenticated and an authenticated maintenance handshake succeeds" \
  "the unauth-401 gate credits the authed handshake (auth-gated, not a canned 200); discovery-200 credits the 401"
register mcp_roundtrip check_mcp_roundtrip \
  "/mcp completes a JSON-RPC initialize + tools/list returning a well-formed result" \
  "an unknown method must come back a JSON-RPC error (requests are processed, not blanket-statused); credited only with discovery-200 held"
register edge_authn_backend check_edge_authn_backend \
  "authenticated /sage_vaults 200 with a JSON-array body" \
  "a backend-shaped array distinguishes a real backend from a canned edge 200"
register liveness check_liveness \
  "/health 200 status=ok" \
  "store-free endpoint; credited only as process-up, not store/vault readiness"
register ocr_capability check_ocr_capability \
  "/health advertises ocr with ocrmypdf, tesseract, and ghostscript all true (the cloud image ships the OCR toolchain)" \
  "a stale image predating the ocr field, or any binary false, FAILs -- the field is the container's real find_spec/which probe, not a canned 200"
register transfer_upload_gate check_transfer_upload_gate \
  "tokenless PUT /upload answers the app's 410 transfer_token_invalid" \
  "410 (not 401/404) proves the dedicated operation + no-base policy + app token gate together"
register transfer_download_gate check_transfer_download_gate \
  "tokenless GET /download/{id} answers the app's 410 transfer_token_invalid" \
  "410 (not 401/404) proves the templated operation + no-base policy + app token gate together"
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
register bff_custom_domain_tls check_bff_custom_domain_tls \
  "cas custom domain serves a complete, trusted certificate chain (leaf + intermediate, verify 0)" \
  "a leaf-only chain with a valid SAN must FAIL -- ACA serves the env-cert PFX verbatim, so a missing intermediate fails strict clients (curl 60) though the leaf is correct"
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
  HTTP_DIAG=""
  set +e
  "$fn"
  rc=$?
  set -e
  case "$rc" in
    0) status=PASS ;;
    2) status=SKIP ;;
    *) status=FAIL ;;
  esac
  # A connection-level failure (000) leaves HTTP_DIAG naming curl's decoded exit
  # reason; surface it on the FAIL line so an opaque 000 becomes a diagnosis.
  # Empty on a real HTTP status, so a genuine 4xx/5xx is never mislabeled.
  if [ "$status" = FAIL ] && [ -n "$HTTP_DIAG" ]; then
    DETAIL_MSG="$DETAIL_MSG [last probe: $HTTP_DIAG]"
  fi
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
PREFLIGHT_TLS_CHAIN_PROBE_CMD="${PREFLIGHT_TLS_CHAIN_PROBE_CMD:-default_tls_chain_probe}"
PREFLIGHT_CURL_CMD="${PREFLIGHT_CURL_CMD:-curl}"
PREFLIGHT_MCP_PROBE_CMD="${PREFLIGHT_MCP_PROBE_CMD:-python3 $SCRIPT_DIR/mcp_preflight_probe.py}"
PREFLIGHT_SLEEP_CMD="${PREFLIGHT_SLEEP_CMD:-sleep}"
PREFLIGHT_WARMUP_MAX_ATTEMPTS="${PREFLIGHT_WARMUP_MAX_ATTEMPTS:-36}"
PREFLIGHT_WARMUP_INTERVAL_SECONDS="${PREFLIGHT_WARMUP_INTERVAL_SECONDS:-5}"
EXPECTED_SAGE_CNAME_SUFFIX="${EXPECTED_SAGE_CNAME_SUFFIX:-azure-api.net}"
EXPECTED_CAS_CNAME_SUFFIX="${EXPECTED_CAS_CNAME_SUFFIX:-azurecontainerapps.io}"

WORKDIR="$(mktemp -d 2>/dev/null || mktemp -d -t cas-preflight)"
trap 'rm -rf "$WORKDIR"' EXIT

main
