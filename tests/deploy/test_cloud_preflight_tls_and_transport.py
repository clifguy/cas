"""The preflight's DNS, TLS and transport checks.

Covers DNS targets, certificate subjects and the BFF custom domain's served
chain, the warm-up retry budget for a connection-level ``000``, the bearer-token
claims diagnostic, the decode of a ``000`` into its cause, and the MCP-surface
checks.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

from tests.deploy._preflight_harness import (
    _HTTP_CHECKS,
    _NEEDS_BASH,
    _NEEDS_RUNTIME,
    _base_env,
    _detail,
    _green,
    _run,
    _verdicts,
    _write_chain_probe_stub,
    _write_probe_stub,
    _write_stub_cmd,
    serve,
)


@_NEEDS_RUNTIME
def test_dns_wrong_target_fails(tmp_path: Path) -> None:
    """A CNAME that resolves to a parked/wrong target fails, even though it
    'resolves' -- the expected-suffix control catches it.
    """
    resolver = _write_stub_cmd(
        tmp_path,
        "resolve",
        'echo "parked.example.com."\n',  # every query -> wrong target
    )
    env = _base_env(
        "http://127.0.0.1:1",  # unused: only DNS checks selected
        PREFLIGHT_CHECKS="dns_sage_cname,dns_cas_cname",
        PREFLIGHT_RESOLVE_CMD=resolver,
    )
    proc = _run(env)
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode != 0
    assert verdicts.get("dns_sage_cname") == "FAIL", verdicts
    assert verdicts.get("dns_cas_cname") == "FAIL", verdicts


@_NEEDS_RUNTIME
def test_dns_wildcard_negative_control_fails(tmp_path: Path) -> None:
    """A wildcard resolver that answers the expected target for *every* name --
    including the deliberately-bogus control name -- must FAIL: the success is
    canned, not a real record.
    """
    resolver = _write_stub_cmd(
        tmp_path,
        "resolve",
        # Echo the expected Azure suffix for ANY name, bogus control included.
        'echo "cas-edge.azure-api.net."\n',
    )
    env = _base_env(
        "http://127.0.0.1:1",
        PREFLIGHT_CHECKS="dns_sage_cname",
        PREFLIGHT_RESOLVE_CMD=resolver,
        EXPECTED_SAGE_CNAME_SUFFIX="azure-api.net",
    )
    proc = _run(env)
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("dns_sage_cname") == "FAIL", (
        "a resolver that answers the bogus control name is wildcarding; the "
        f"expected-target match must not be credited: {verdicts}"
    )


@_NEEDS_RUNTIME
def test_dns_resolves_to_expected_passes(tmp_path: Path) -> None:
    """Sanity: the real name resolves to the expected suffix and the bogus
    control name resolves to nothing -> PASS.
    """
    resolver = _write_stub_cmd(
        tmp_path,
        "resolve",
        # $1 = name, $2 = type. The bogus control label resolves to NXDOMAIN
        # (empty); the real name resolves to the expected Azure suffix.
        'case "$1" in\n'
        "  *nxdomain-control*) exit 0 ;;\n"
        '  *) echo "cas-edge.azure-api.net." ;;\n'
        "esac\n",
    )
    env = _base_env(
        "http://127.0.0.1:1",
        PREFLIGHT_CHECKS="dns_sage_cname",
        PREFLIGHT_RESOLVE_CMD=resolver,
        EXPECTED_SAGE_CNAME_SUFFIX="azure-api.net",
    )
    proc = _run(env)
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("dns_sage_cname") == "PASS", verdicts


@_NEEDS_RUNTIME
def test_tls_wrong_subject_fails(tmp_path: Path) -> None:
    """A handshake that succeeds behind a cert whose SAN does not cover
    ``*.<base-domain>`` must FAIL -- 'handshake worked' is not enough.
    """
    tls = _write_stub_cmd(tmp_path, "tlsprobe", 'echo "DNS:*.wrong.example.com"\n')
    env = _base_env(
        "http://127.0.0.1:1",
        PREFLIGHT_CHECKS="kv_wildcard_tls",
        PREFLIGHT_TLS_PROBE_CMD=tls,
    )
    proc = _run(env)
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode != 0
    assert verdicts.get("kv_wildcard_tls") == "FAIL", verdicts


@_NEEDS_RUNTIME
def test_tls_matching_subject_passes(tmp_path: Path) -> None:
    tls = _write_stub_cmd(tmp_path, "tlsprobe", 'echo "DNS:*.test.invalid"\n')
    env = _base_env(
        "http://127.0.0.1:1",
        PREFLIGHT_CHECKS="kv_wildcard_tls",
        PREFLIGHT_TLS_PROBE_CMD=tls,
    )
    proc = _run(env)
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("kv_wildcard_tls") == "PASS", verdicts


# --------------------------------------------------------------------------- #
# BFF custom-domain chain completeness (cas.<domain> must serve a full chain)  #
# --------------------------------------------------------------------------- #
# kv_wildcard_tls checks only the leaf SAN at the APIM edge (sage.<domain>); it
# never looks at the BFF custom domain's served chain. ACA serves the bound
# env-certificate's PFX bytes verbatim, so a leaf-only PFX makes cas.<domain>
# fail strict clients (curl 60) while APIM masks it for sage.<domain> by
# rebuilding the chain. This check probes the BFF host's served chain directly.


@_NEEDS_RUNTIME
def test_bff_custom_domain_tls_complete_chain_passes(tmp_path: Path) -> None:
    """A complete chain (>=2 certs) that verifies (openssl code 0) PASSes."""
    probe = _write_chain_probe_stub(tmp_path, "4 0")
    env = _base_env(
        "http://127.0.0.1:1",
        PREFLIGHT_CHECKS="bff_custom_domain_tls",
        PREFLIGHT_TLS_CHAIN_PROBE_CMD=probe,
    )
    proc = _run(env)
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert verdicts.get("bff_custom_domain_tls") == "PASS", proc.stdout


@_NEEDS_RUNTIME
def test_bff_custom_domain_tls_leaf_only_fails(tmp_path: Path) -> None:
    """THE anti-coincidental trap: a leaf with the CORRECT SAN but no intermediate
    (1 cert served) must FAIL. ACA serves the PFX verbatim, so strict clients
    reject it (curl 60) though the leaf is valid -- exactly the case a SAN-only
    check (kv_wildcard_tls) would wrongly wave through.
    """
    probe = _write_chain_probe_stub(tmp_path, "1 21")
    env = _base_env(
        "http://127.0.0.1:1",
        PREFLIGHT_CHECKS="bff_custom_domain_tls",
        PREFLIGHT_TLS_CHAIN_PROBE_CMD=probe,
    )
    proc = _run(env)
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode != 0, f"a leaf-only chain must fail the run:\n{proc.stdout}"
    assert verdicts.get("bff_custom_domain_tls") == "FAIL", proc.stdout
    detail = _detail(proc.stdout, "bff_custom_domain_tls")
    assert re.search(r"incomplete|intermediate|leaf", detail, re.I), (
        f"the leaf-only failure must name the missing intermediate: {detail!r}"
    )


@_NEEDS_RUNTIME
def test_bff_custom_domain_tls_untrusted_chain_fails(tmp_path: Path) -> None:
    """A chain that is present (>=2 certs) but does not verify (openssl code != 0)
    must FAIL -- a distinct failure from the leaf-only case.
    """
    probe = _write_chain_probe_stub(tmp_path, "2 20")
    env = _base_env(
        "http://127.0.0.1:1",
        PREFLIGHT_CHECKS="bff_custom_domain_tls",
        PREFLIGHT_TLS_CHAIN_PROBE_CMD=probe,
    )
    proc = _run(env)
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode != 0, proc.stdout
    assert verdicts.get("bff_custom_domain_tls") == "FAIL", proc.stdout
    detail = _detail(proc.stdout, "bff_custom_domain_tls")
    assert re.search(r"verif|trust|20", detail, re.I), (
        f"a present-but-untrusted chain must say so (not 'incomplete'): {detail!r}"
    )


@_NEEDS_RUNTIME
def test_bff_custom_domain_tls_no_cert_fails(tmp_path: Path) -> None:
    """No certificate served (handshake produced nothing) must FAIL."""
    probe = _write_chain_probe_stub(tmp_path, "0 99")
    env = _base_env(
        "http://127.0.0.1:1",
        PREFLIGHT_CHECKS="bff_custom_domain_tls",
        PREFLIGHT_TLS_CHAIN_PROBE_CMD=probe,
    )
    proc = _run(env)
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode != 0, proc.stdout
    assert verdicts.get("bff_custom_domain_tls") == "FAIL", proc.stdout


@_NEEDS_RUNTIME
def test_bff_auth_configured_passes() -> None:
    with serve(_green) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="bff_liveness,bff_auth_configured"))
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert verdicts.get("bff_liveness") == "PASS", verdicts
    assert verdicts.get("bff_auth_configured") == "PASS", verdicts


@_NEEDS_RUNTIME
def test_bff_auth_unconfigured_fails() -> None:
    """If /app/auth/me reports auth_not_configured (503), the BFF is up but its
    OIDC config did not resolve -> bff_auth_configured FAILs.
    """

    def unconfigured(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        p = path.split("?", 1)[0]
        if p == "/app/auth/me":
            return 503, '{"error":"auth_not_configured"}', {}
        if p == "/app/auth/login":
            return 503, '{"error":"auth_not_configured"}', {}
        return _green(method, path, body)

    with serve(unconfigured) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="bff_liveness,bff_auth_configured"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("bff_liveness") == "PASS", "the BFF process is still up"
    assert verdicts.get("bff_auth_configured") == "FAIL", verdicts
    assert proc.returncode != 0


# --------------------------------------------------------------------------- #
# E. Warm-up retry gate (connection-level 000 only)                           #
# --------------------------------------------------------------------------- #
# A freshly-activated container revision can be briefly unroutable: curl fails
# to connect and the probe records HTTP_CODE 000. The preflight retries that --
# and ONLY that -- within a bounded, shared budget, so a transient readiness
# window does not flake the gate while a genuine outage (or any real 4xx/5xx)
# still fails it promptly. These tests drive the behavior offline through the
# PREFLIGHT_CURL_CMD / PREFLIGHT_SLEEP_CMD seams.


def _write_curl_stub(tmp_path: Path, counter: Path, fail_until: int, exit_code: int = 7) -> str:
    """A curl seam that simulates a not-yet-routable endpoint.

    Each invocation increments ``counter`` (so a test can assert the exact probe
    count). For the first ``fail_until`` calls it exits non-zero like curl's
    "couldn't connect" (the script maps that to HTTP_CODE 000); thereafter it
    ``exec``s the real curl, which serves the live stub server's real response.
    Set ``fail_until`` huge to model a persistent outage.

    ``exit_code`` is the curl exit status to simulate (default 7, "failed to
    connect"). Pass a different code to model a distinct failure mode the script
    must decode -- e.g. 35 (TLS handshake), 6 (DNS), 28 (timeout), 60 (cert).
    """
    body = (
        f'n=$(cat "{counter}" 2>/dev/null || echo 0)\n'
        "n=$((n + 1))\n"
        f'echo "$n" > "{counter}"\n'
        f'if [ "$n" -le {fail_until} ]; then exit {exit_code}; fi\n'
        'exec curl "$@"\n'
    )
    return _write_stub_cmd(tmp_path, "curl-stub", body)


def _write_sleep_tripwire(tmp_path: Path, log: Path) -> str:
    """A sleep seam that appends a line per call (never actually sleeps), so a
    test can assert the exact retry count -- or assert zero retries by the log's
    absence.
    """
    return _write_stub_cmd(tmp_path, "sleep-stub", f'echo x >> "{log}"\nexit 0\n')


@_NEEDS_RUNTIME
def test_warmup_retries_connection_failure_until_ready(tmp_path: Path) -> None:
    """A cold endpoint (000) that becomes routable inside the budget PASSes:
    the probe fails 3x, the warm-up retries, and the 4th attempt reaches the
    healthy stub.
    """
    counter = tmp_path / "calls"
    curl = _write_curl_stub(tmp_path, counter, fail_until=3)
    with serve(_green) as url:
        proc = _run(
            _base_env(
                url,
                PREFLIGHT_CHECKS="bff_liveness",
                PREFLIGHT_CURL_CMD=curl,
                PREFLIGHT_WARMUP_MAX_ATTEMPTS="10",
                PREFLIGHT_WARMUP_INTERVAL_SECONDS="0",
            )
        )
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert verdicts.get("bff_liveness") == "PASS", verdicts


@_NEEDS_RUNTIME
def test_warmup_budget_too_small_still_fails(tmp_path: Path) -> None:
    """The budget is a real cap: an endpoint that would recover only after more
    attempts than the budget allows still FAILs, and the probe is invoked
    exactly 1 (initial) + MAX_ATTEMPTS times.
    """
    counter = tmp_path / "calls"
    curl = _write_curl_stub(tmp_path, counter, fail_until=5)
    with serve(_green) as url:
        proc = _run(
            _base_env(
                url,
                PREFLIGHT_CHECKS="bff_liveness",
                PREFLIGHT_CURL_CMD=curl,
                PREFLIGHT_WARMUP_MAX_ATTEMPTS="2",
                PREFLIGHT_WARMUP_INTERVAL_SECONDS="0",
            )
        )
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode != 0, f"{proc.stdout}\n{proc.stderr}"
    assert verdicts.get("bff_liveness") == "FAIL", verdicts
    assert counter.read_text().strip() == "3", "expected 1 initial + 2 retries"


@_NEEDS_RUNTIME
def test_warmup_disabled_preserves_single_shot(tmp_path: Path) -> None:
    """With the budget off (MAX_ATTEMPTS=0) the probe is single-shot, exactly as
    before this feature: one connection failure, one attempt, FAIL.
    """
    counter = tmp_path / "calls"
    curl = _write_curl_stub(tmp_path, counter, fail_until=1)
    with serve(_green) as url:
        proc = _run(
            _base_env(
                url,
                PREFLIGHT_CHECKS="bff_liveness",
                PREFLIGHT_CURL_CMD=curl,
                PREFLIGHT_WARMUP_MAX_ATTEMPTS="0",
                PREFLIGHT_WARMUP_INTERVAL_SECONDS="0",
            )
        )
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("bff_liveness") == "FAIL", verdicts
    assert counter.read_text().strip() == "1", "budget off must mean a single probe"


@_NEEDS_RUNTIME
def test_warmup_bounded_failure_terminates(tmp_path: Path) -> None:
    """A persistent outage (always 000) fails the gate within the bound and does
    not loop forever: the probe runs 1 + MAX_ATTEMPTS times and the retry sleep
    fires exactly MAX_ATTEMPTS times.
    """
    counter = tmp_path / "calls"
    sleeplog = tmp_path / "sleeps"
    curl = _write_curl_stub(tmp_path, counter, fail_until=999)
    sleep = _write_sleep_tripwire(tmp_path, sleeplog)
    proc = _run(
        _base_env(
            "http://127.0.0.1:1",
            PREFLIGHT_CHECKS="bff_liveness",
            PREFLIGHT_CURL_CMD=curl,
            PREFLIGHT_SLEEP_CMD=sleep,
            PREFLIGHT_WARMUP_MAX_ATTEMPTS="3",
            PREFLIGHT_WARMUP_INTERVAL_SECONDS="0",
        )
    )
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode != 0, f"{proc.stdout}\n{proc.stderr}"
    assert verdicts.get("bff_liveness") == "FAIL", verdicts
    assert counter.read_text().strip() == "4", "expected 1 initial + 3 retries"
    assert sleeplog.read_text().count("x") == 3, "one sleep per retry"


@_NEEDS_RUNTIME
def test_warmup_does_not_retry_http_error(tmp_path: Path) -> None:
    """A real 5xx (a genuine server fault, not a connection failure) fails fast:
    the retry NEVER engages, so the sleep seam is never invoked. The code path is
    identical for 4xx, so this covers both.
    """
    sleeplog = tmp_path / "sleeps"
    sleep = _write_sleep_tripwire(tmp_path, sleeplog)

    def health_500(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0] == "/health":
            return 500, '{"error":"unavailable"}', {}
        return _green(method, path, body)

    with serve(health_500) as url:
        proc = _run(
            _base_env(
                url,
                PREFLIGHT_CHECKS="bff_liveness",
                PREFLIGHT_SLEEP_CMD=sleep,
                PREFLIGHT_WARMUP_MAX_ATTEMPTS="24",
                PREFLIGHT_WARMUP_INTERVAL_SECONDS="0",
            )
        )
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode != 0, f"{proc.stdout}\n{proc.stderr}"
    assert verdicts.get("bff_liveness") == "FAIL", verdicts
    assert not sleeplog.exists(), "a real 5xx must not be retried"


@_NEEDS_RUNTIME
def test_warmup_silent_on_healthy_tenant(tmp_path: Path) -> None:
    """The happy path pays no warm-up cost: every probe answers immediately, so
    the retry sleep is never invoked even though the budget is large.
    """
    sleeplog = tmp_path / "sleeps"
    sleep = _write_sleep_tripwire(tmp_path, sleeplog)
    with serve(_green) as url:
        proc = _run(
            _base_env(
                url,
                PREFLIGHT_CHECKS=_HTTP_CHECKS,
                PREFLIGHT_SLEEP_CMD=sleep,
                PREFLIGHT_WARMUP_MAX_ATTEMPTS="24",
                PREFLIGHT_WARMUP_INTERVAL_SECONDS="0",
            )
        )
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert all(v == "PASS" for v in verdicts.values()), verdicts
    assert not sleeplog.exists(), "a healthy tenant must trigger no retries"


@_NEEDS_RUNTIME
def test_warmup_budget_shared_across_checks(tmp_path: Path) -> None:
    """The budget is global, not per-probe: under a total outage the first check
    consumes the whole budget and every later 000 fails fast. Two checks ->
    sleep fires MAX_ATTEMPTS times total (not 2x), and the probe runs
    1 + MAX_ATTEMPTS (first check) + 1 (second check, budget already spent).
    """
    counter = tmp_path / "calls"
    sleeplog = tmp_path / "sleeps"
    curl = _write_curl_stub(tmp_path, counter, fail_until=999)
    sleep = _write_sleep_tripwire(tmp_path, sleeplog)
    proc = _run(
        _base_env(
            "http://127.0.0.1:1",
            PREFLIGHT_CHECKS="liveness,bff_liveness",
            PREFLIGHT_CURL_CMD=curl,
            PREFLIGHT_SLEEP_CMD=sleep,
            PREFLIGHT_WARMUP_MAX_ATTEMPTS="3",
            PREFLIGHT_WARMUP_INTERVAL_SECONDS="0",
        )
    )
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode != 0, f"{proc.stdout}\n{proc.stderr}"
    assert verdicts.get("liveness") == "FAIL", verdicts
    assert verdicts.get("bff_liveness") == "FAIL", verdicts
    assert sleeplog.read_text().count("x") == 3, "the budget is shared, not per-check"
    assert counter.read_text().strip() == "5", "1 + 3 (first check) + 1 (second, spent)"


# --------------------------------------------------------------------------- #
# F. Token-claims diagnostic (decode iss/aud/ver of the bearer token)         #
# --------------------------------------------------------------------------- #
_DIAG_GUID = "11111111-2222-3333-4444-555555555555"


def _synthetic_jwt(**claims: object) -> str:
    """A structurally-valid (unsigned) JWT; only the payload segment is decoded."""

    def _seg(obj: dict[str, object]) -> str:
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return f"{_seg({'alg': 'none', 'typ': 'JWT'})}.{_seg(dict(claims))}.signature"


def _diag_env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    """Minimal env that runs main() with one offline check -- diagnostic only.

    A selection must leave at least one check to run, so the run selects the
    asuid TXT check against a stub resolver that answers the real name and not
    the negative control: main() emits the diagnostic, the check passes without
    a network call, and the run exits 0.
    """
    resolver = _write_stub_cmd(
        tmp_path,
        "resolve",
        'case "$1" in *nxdomain-control*) ;; *) echo \'"verification-token"\' ;; esac\n',
    )
    env = {
        "SAGE_FQDN": "sage.test.invalid",
        "BASE_DOMAIN": "test.invalid",
        "AUTH_TOKEN": "test-token",
        "PREFLIGHT_CHECKS": "dns_asuid_txt",
        "PREFLIGHT_RESOLVE_CMD": resolver,
    }
    env.update(overrides)
    return env


@_NEEDS_BASH
def test_diagnostic_decodes_jwt_iss_aud_ver(tmp_path: Path) -> None:
    # A real (synthetic) JWT: the diagnostic reports the *decoded* claim values,
    # which only appear if the payload segment was actually base64url-decoded.
    token = _synthetic_jwt(
        iss="https://login.microsoftonline.com/tid/v2.0", aud=_DIAG_GUID, ver="2.0"
    )
    proc = _run(_diag_env(tmp_path, AUTH_TOKEN=token))
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert f"aud={_DIAG_GUID}" in proc.stderr, proc.stderr
    assert "iss=https://login.microsoftonline.com/tid/v2.0" in proc.stderr, proc.stderr
    assert "ver=2.0" in proc.stderr, proc.stderr


@_NEEDS_BASH
def test_diagnostic_degrades_on_non_jwt_token(tmp_path: Path) -> None:
    # The default AUTH_TOKEN is not a 3-segment JWT; the decoder must degrade
    # gracefully (not crash under `set -euo pipefail`) and the run must complete.
    proc = _run(_diag_env(tmp_path))  # AUTH_TOKEN="test-token"
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "token-claims: unavailable" in proc.stderr, proc.stderr
    assert "=== preflight complete" in proc.stderr, "the run must reach completion"


@_NEEDS_BASH
def test_diagnostic_never_prints_raw_token(tmp_path: Path) -> None:
    token = _synthetic_jwt(
        iss="https://login.microsoftonline.com/tid/v2.0", aud=_DIAG_GUID, ver="2.0"
    )
    proc = _run(_diag_env(tmp_path, AUTH_TOKEN=token))
    assert token not in (proc.stdout + proc.stderr), "the raw bearer token must never be logged"


# --------------------------------------------------------------------------- #
# G. Connection-level 000 reason decode (turn an opaque 000 into a diagnosis)  #
# --------------------------------------------------------------------------- #
# A 000 is a connection-level failure that does not by itself distinguish "not
# yet routable" from "TLS handshake refused" from "DNS". The probe captures
# curl's exit code, decodes it to a human reason, and surfaces it on the FAIL
# line -- so the next red deploy names its own cause instead of guessing. These
# drive the decode offline through the PREFLIGHT_CURL_CMD seam (curl-stub exit
# codes) and read the message back via ``_detail``.


@_NEEDS_RUNTIME
def test_curl_reason_surfaced_on_connection_refused(tmp_path: Path) -> None:
    """A terminal 000 from a connection refusal (curl exit 7) decorates the FAIL
    line with the decoded reason. Today's script collapses every non-zero curl
    exit to a bare ``code=000``; this can only pass once the exit code is
    captured and named.
    """
    counter = tmp_path / "calls"
    curl = _write_curl_stub(tmp_path, counter, fail_until=999, exit_code=7)
    proc = _run(
        _base_env(
            "http://127.0.0.1:1",
            PREFLIGHT_CHECKS="bff_liveness",
            PREFLIGHT_CURL_CMD=curl,
            PREFLIGHT_WARMUP_MAX_ATTEMPTS="2",
            PREFLIGHT_WARMUP_INTERVAL_SECONDS="0",
        )
    )
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("bff_liveness") == "FAIL", verdicts
    detail = _detail(proc.stdout, "bff_liveness")
    assert "curl 7" in detail, f"the decoded curl exit code must name the failure mode: {detail!r}"
    assert re.search(r"connection|routable|refus", detail, re.I), (
        f"the reason must carry a human phrase, not just the bare code: {detail!r}"
    )


@_NEEDS_RUNTIME
def test_curl_reason_distinguishes_tls_handshake(tmp_path: Path) -> None:
    """THE discriminating decode: a 000 from a TLS handshake failure (curl 35)
    is named distinctly from a not-yet-routable connection refusal (curl 7).
    A naive fix that prints one generic "connection failure" for every non-zero
    exit would make these indistinguishable; paired with the curl-7 test this
    pins a code-varying mapping -- the actual diagnostic value.
    """
    counter = tmp_path / "calls"
    curl = _write_curl_stub(tmp_path, counter, fail_until=999, exit_code=35)
    proc = _run(
        _base_env(
            "http://127.0.0.1:1",
            PREFLIGHT_CHECKS="bff_liveness",
            PREFLIGHT_CURL_CMD=curl,
            PREFLIGHT_WARMUP_MAX_ATTEMPTS="2",
            PREFLIGHT_WARMUP_INTERVAL_SECONDS="0",
        )
    )
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("bff_liveness") == "FAIL", verdicts
    detail = _detail(proc.stdout, "bff_liveness")
    assert "curl 35" in detail, f"the TLS failure mode must be named: {detail!r}"
    assert re.search(r"tls|ssl|handshake", detail, re.I), (
        f"curl 35 must decode to a TLS/handshake phrase, distinct from a routing reason: {detail!r}"
    )


@_NEEDS_RUNTIME
def test_curl_reason_absent_on_real_http_error() -> None:
    """A genuine 5xx (curl exits 0; a real status arrived) carries NO curl
    reason: the decode attaches only to connection-level failures and never
    mislabels a real server fault as a connection problem. Guards against an
    over-eager fix that always appends a reason.
    """

    def health_500(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0] == "/health":
            return 500, '{"error":"unavailable"}', {}
        return _green(method, path, body)

    with serve(health_500) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="bff_liveness"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("bff_liveness") == "FAIL", verdicts
    detail = _detail(proc.stdout, "bff_liveness")
    assert "curl " not in detail, (
        f"a real HTTP failure must not be tagged with a curl reason: {detail!r}"
    )
    assert "code=500" in detail, f"the real status must still be reported: {detail!r}"


@_NEEDS_RUNTIME
@pytest.mark.parametrize(
    ("exit_code", "phrase"),
    [(6, r"dns|resolve"), (28, r"time|timeout"), (60, r"cert|tls|ssl")],
)
def test_curl_reason_mapping_covers_failure_modes(
    tmp_path: Path, exit_code: int, phrase: str
) -> None:
    """The decode table names each diagnostically-distinct curl failure mode and
    every reason embeds the literal ``curl <code>`` for an unambiguous anchor --
    so a future edit that drops a row is caught.
    """
    counter = tmp_path / "calls"
    curl = _write_curl_stub(tmp_path, counter, fail_until=999, exit_code=exit_code)
    proc = _run(
        _base_env(
            "http://127.0.0.1:1",
            PREFLIGHT_CHECKS="bff_liveness",
            PREFLIGHT_CURL_CMD=curl,
            PREFLIGHT_WARMUP_MAX_ATTEMPTS="1",
            PREFLIGHT_WARMUP_INTERVAL_SECONDS="0",
        )
    )
    detail = _detail(proc.stdout, "bff_liveness")
    assert f"curl {exit_code}" in detail, (
        f"exit {exit_code} must be named on the FAIL line: {detail!r}"
    )
    assert re.search(phrase, detail, re.I), f"exit {exit_code} reason phrase missing: {detail!r}"


@_NEEDS_RUNTIME
def test_warmup_engage_breadcrumb_to_stderr(tmp_path: Path) -> None:
    """When the warm-up engages, a single breadcrumb to STDERR names the decoded
    reason, so a human watching the live CI log understands the bounded pause.
    The stdout matrix is untouched -- the verdict parser sees nothing new.
    """
    counter = tmp_path / "calls"
    curl = _write_curl_stub(tmp_path, counter, fail_until=999, exit_code=7)
    proc = _run(
        _base_env(
            "http://127.0.0.1:1",
            PREFLIGHT_CHECKS="bff_liveness",
            PREFLIGHT_CURL_CMD=curl,
            PREFLIGHT_WARMUP_MAX_ATTEMPTS="2",
            PREFLIGHT_WARMUP_INTERVAL_SECONDS="0",
        )
    )
    assert re.search(r"warm-up", proc.stderr, re.I), (
        f"no warm-up breadcrumb on stderr: {proc.stderr!r}"
    )
    assert "curl 7" in proc.stderr, f"the breadcrumb must name the decoded reason: {proc.stderr!r}"


# --------------------------------------------------------------------------- #
# E. MCP-surface checks (maintenance-mount auth enforcement + mcp_roundtrip)   #
#                                                                              #
# The bash check owns the anti-coincidental CONTROL logic (discovery-200 edge- #
# live + the unauth-401 gate); the round-trip PROTOCOL is stubbed here via the #
# PREFLIGHT_MCP_PROBE_CMD seam and proven for real in test_mcp_preflight_probe. #
# The maintenance mount is /mcp_maint; Admin retirement has a separate check. #
# --------------------------------------------------------------------------- #
def _discovery_broken(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
    """``_green`` but the OAuth discovery doc 404s -- the dead-edge control trap."""
    if path.split("?", 1)[0] == "/.well-known/oauth-protected-resource":
        return 404, '{"error":"not_found"}', {}
    return _green(method, path, body)


_MAINTENANCE_CHECKS = [("mcp_maint", "/mcp_maint")]


@_NEEDS_RUNTIME
@pytest.mark.parametrize(("check", "mount"), _MAINTENANCE_CHECKS)
def test_maintenance_mount_passes_when_401_unauth_and_authed_probe_ok(
    tmp_path: Path, check: str, mount: str
) -> None:
    """Healthy maintenance mount: 401 unauth + an authenticated handshake that
    succeeds, with the discovery-200 control held -> PASS.
    """
    probe = _write_probe_stub(tmp_path, f"mode=handshake mount={mount} initialize=ok", 0)
    with serve(_green) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS=check, PREFLIGHT_MCP_PROBE_CMD=probe))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get(check) == "PASS", proc.stdout
    assert proc.returncode == 0


@_NEEDS_RUNTIME
@pytest.mark.parametrize(("check", "mount"), _MAINTENANCE_CHECKS)
def test_maintenance_mount_fails_when_discovery_broken_but_mount_401(
    tmp_path: Path, check: str, mount: str
) -> None:
    """THE blanket-edge trap for the maintenance mount: the mount answers 401 and
    the authed probe succeeds, but the discovery-200 control failed (404) -- the
    401 must NOT be credited on a dead edge.
    """
    probe = _write_probe_stub(tmp_path, f"mode=handshake mount={mount} initialize=ok", 0)
    with serve(_discovery_broken) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS=check, PREFLIGHT_MCP_PROBE_CMD=probe))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get(check) == "FAIL", (
        f"a 401 must not be credited when the discovery-200 control failed: {proc.stdout}"
    )
    assert proc.returncode != 0


@_NEEDS_RUNTIME
@pytest.mark.parametrize(("check", "mount"), _MAINTENANCE_CHECKS)
def test_maintenance_mount_fails_when_unauth_returns_200(
    tmp_path: Path, check: str, mount: str
) -> None:
    """Auth not enforced on the maintenance mount: the mount answers 200 unauth.
    Even with a passing authed probe, the missing 401 must FAIL the check.
    """
    probe = _write_probe_stub(tmp_path, f"mode=handshake mount={mount} initialize=ok", 0)

    def mount_open(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0] == mount:
            return 200, '{"oops":"unauthenticated reached maintenance"}', {}
        return _green(method, path, body)

    with serve(mount_open) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS=check, PREFLIGHT_MCP_PROBE_CMD=probe))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get(check) == "FAIL", f"an open {mount} must fail auth-enforcement"
    assert proc.returncode != 0


@_NEEDS_RUNTIME
@pytest.mark.parametrize(("check", "mount"), _MAINTENANCE_CHECKS)
def test_maintenance_mount_fails_when_authed_probe_fails(
    tmp_path: Path, check: str, mount: str
) -> None:
    """401 unauth + discovery-200 both hold, but the authenticated maintenance
    handshake fails (probe exit 1) -> FAIL. A check that asserted only the unauth
    401 would coincidentally pass here.
    """
    probe = _write_probe_stub(tmp_path, f"mode=handshake mount={mount} initialize=fail", 1)
    with serve(_green) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS=check, PREFLIGHT_MCP_PROBE_CMD=probe))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get(check) == "FAIL", proc.stdout
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_mcp_roundtrip_passes_when_probe_ok(tmp_path: Path) -> None:
    """Discovery-200 held + the round-trip probe reports success -> PASS."""
    probe = _write_probe_stub(
        tmp_path, "mode=roundtrip mount=/mcp initialize=ok tools_list=ok negctrl=error", 0
    )
    with serve(_green) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="mcp_roundtrip", PREFLIGHT_MCP_PROBE_CMD=probe))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("mcp_roundtrip") == "PASS", proc.stdout
    assert proc.returncode == 0


@_NEEDS_RUNTIME
def test_mcp_roundtrip_fails_when_probe_fails(tmp_path: Path) -> None:
    """Discovery-200 held but the probe reports failure (e.g. the negative control
    did not discriminate) -> FAIL. A check that ignored the probe would pass.
    """
    probe = _write_probe_stub(tmp_path, "mode=roundtrip mount=/mcp negctrl=fail", 1)
    with serve(_green) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="mcp_roundtrip", PREFLIGHT_MCP_PROBE_CMD=probe))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("mcp_roundtrip") == "FAIL", proc.stdout
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_mcp_roundtrip_fails_when_discovery_broken(tmp_path: Path) -> None:
    """THE dead-edge trap for the round-trip: the probe reports success but the
    discovery-200 control failed (404) -- a round-trip result on a dead edge must
    not be credited.
    """
    probe = _write_probe_stub(
        tmp_path, "mode=roundtrip mount=/mcp initialize=ok tools_list=ok negctrl=error", 0
    )
    with serve(_discovery_broken) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="mcp_roundtrip", PREFLIGHT_MCP_PROBE_CMD=probe))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("mcp_roundtrip") == "FAIL", (
        f"round-trip success must not be credited on a dead edge: {proc.stdout}"
    )
    assert proc.returncode != 0


# --------------------------------------------------------------------------- #
# Host defaults, and matches anchored at their boundaries                     #
# --------------------------------------------------------------------------- #
@_NEEDS_RUNTIME
def test_cas_host_defaults_from_the_base_domain_derived_from_sage_fqdn(tmp_path: Path) -> None:
    """The header documents ``SAGE_FQDN or BASE_DOMAIN``; with only the former
    set, the CAS host derives from the domain SAGE_FQDN names."""
    asked = tmp_path / "asked"
    resolver = _write_stub_cmd(tmp_path, "resolve", f'echo "$1" >> "{asked}"\n')
    env = _base_env(
        "http://127.0.0.1:1", PREFLIGHT_CHECKS="dns_cas_cname", PREFLIGHT_RESOLVE_CMD=resolver
    )
    env["SAGE_FQDN"] = "sage.example.test"
    for unset in ("CAS_FQDN", "BASE_DOMAIN"):
        del env[unset]
    proc = _run(env)
    names = asked.read_text(encoding="utf-8").split()
    assert "cas.example.test" in names, names
    assert not any(n == "cas." or n.endswith(".cas.") for n in names), names
    assert "cas=cas.example.test" in proc.stderr, proc.stderr


@pytest.mark.parametrize(
    "san,expected",
    [
        ("DNS:*.test.invalid.parked.net", "FAIL"),
        ("DNS:*.test.invalidity.example", "FAIL"),
        ("DNS:*.test.invalid, DNS:test.invalid", "PASS"),
    ],
    ids=["suffixed-domain", "longer-label", "list-form"],
)
@_NEEDS_RUNTIME
def test_wildcard_san_is_matched_to_the_whole_domain(
    tmp_path: Path, san: str, expected: str
) -> None:
    tls = _write_stub_cmd(tmp_path, "tlsprobe", f'echo "{san}"\n')
    env = _base_env(
        "http://127.0.0.1:1", PREFLIGHT_CHECKS="kv_wildcard_tls", PREFLIGHT_TLS_PROBE_CMD=tls
    )
    proc = _run(env)
    assert _verdicts(proc.stdout).get("kv_wildcard_tls") == expected, proc.stdout


@pytest.mark.parametrize(
    "target,expected",
    [
        ("apim.azure-api.net.evil.example.", "FAIL"),
        ("notazure-api.net.", "FAIL"),
        ("sage-apim.azure-api.net.", "PASS"),
        ("Sage-APIM.Azure-API.net", "PASS"),
    ],
    ids=["suffix-mid-name", "no-label-boundary", "trailing-dot", "mixed-case"],
)
@_NEEDS_RUNTIME
def test_cname_suffix_is_matched_as_a_label_suffix(
    tmp_path: Path, target: str, expected: str
) -> None:
    resolver = _write_stub_cmd(
        tmp_path,
        "resolve",
        f'case "$1" in\n  *nxdomain-control*) exit 0 ;;\n  *) echo "{target}" ;;\nesac\n',
    )
    env = _base_env(
        "http://127.0.0.1:1",
        PREFLIGHT_CHECKS="dns_sage_cname",
        PREFLIGHT_RESOLVE_CMD=resolver,
        EXPECTED_SAGE_CNAME_SUFFIX="azure-api.net",
    )
    proc = _run(env)
    assert _verdicts(proc.stdout).get("dns_sage_cname") == expected, proc.stdout
    assert target in _detail(proc.stdout, "dns_sage_cname"), proc.stdout


@pytest.mark.parametrize(
    "record,expected",
    [
        ('"verification-token-extra"', "FAIL"),
        ('"prefix-verification-token"', "FAIL"),
        ('"verification-token"', "PASS"),
        ('"other"\n"verification-token"', "PASS"),
    ],
    ids=["longer-value", "prefixed-value", "exact", "one-of-several"],
)
@_NEEDS_RUNTIME
def test_asuid_txt_is_matched_as_a_whole_record(tmp_path: Path, record: str, expected: str) -> None:
    resolver = _write_stub_cmd(
        tmp_path,
        "resolve",
        f"case \"$1\" in *nxdomain-control*) ;; *) printf '%s\\n' '{record}' ;; esac\n",
    )
    env = _base_env(
        "http://127.0.0.1:1",
        PREFLIGHT_CHECKS="dns_asuid_txt",
        PREFLIGHT_RESOLVE_CMD=resolver,
        PREFLIGHT_EXPECTED_ASUID="verification-token",
    )
    proc = _run(env)
    assert _verdicts(proc.stdout).get("dns_asuid_txt") == expected, proc.stdout
