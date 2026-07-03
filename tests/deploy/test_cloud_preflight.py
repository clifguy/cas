"""Structural, inventory, and behavioral gate for ``deploy/cloud-preflight.sh``.

The cloud preflight is a *post-deploy* probe: it hits a live deployed tenant's
public HTTPS endpoints plus an operator-supplied bearer token and verifies every
layer independently, emitting a single pass/fail matrix where each check carries
an anti-coincidental control (a negative/expected-failure result is credited only
when a paired positive control proves the edge is genuinely live). It reports; it
never mutates. It is distinct from ``deploy/smoke.sh`` / ``bff-smoke.sh``, which
build+boot a container image locally.

There is no live tenant in CI, so the gate here is two layers, both always-on
with no Docker and no deployed environment:

* **Structural / inventory** -- read the script text and enumerate its check
  registry via ``--dry-run`` (the runtime-wiring inventory a source comment
  cannot fake). Mirrors the ``tests/infra/`` text-assertion idiom.
* **Behavioral** -- stand up a local stub HTTP server plus stub resolver/TLS
  commands, point the script's parameterized endpoints at them, and assert the
  matrix verdicts. The load-bearing scenarios are *blanket-404* (a dead edge
  must not coincidentally pass) and *one-failure-does-not-mask-others* (the
  independence guarantee). These prove the control logic is correct, not merely
  present.
"""

from __future__ import annotations

import base64
import http.server
import inspect
import json
import os
import re
import shutil
import socket
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import pytest

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_SCRIPT: Final[Path] = _REPO_ROOT / "deploy" / "cloud-preflight.sh"

_BASH: Final[str | None] = shutil.which("bash")
_CURL: Final[str | None] = shutil.which("curl")

#: A subscription / tenant / client id is a GUID; none may be baked into the
#: harness -- they arrive as parameters. Same detector the infra gate uses.
_GUID_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b"
)

#: The full check registry the harness must expose (one row per deployment
#: layer). The ``--dry-run`` enumeration must report exactly this set.
_EXPECTED_CHECKS: Final[frozenset[str]] = frozenset(
    {
        "edge_discovery",
        "edge_mcp_unauth",
        "edge_browser_redirect",
        "edge_cors_preflight",
        "edge_dcr_registration",
        "edge_resource_identity",
        "edge_mount_discovery",
        "mcp_admin",
        "mcp_roundtrip",
        "edge_authn_backend",
        "liveness",
        "vault_load",
        "retrieval_pg",
        "kv_wildcard_tls",
        "kv_anthropic",
        "bff_liveness",
        "bff_auth_configured",
        "bff_custom_domain_tls",
        "dns_sage_cname",
        "dns_cas_cname",
        "dns_asuid_txt",
        "sharepoint_discovery",
    }
)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _script_text() -> str:
    return _SCRIPT.read_text(encoding="utf-8")


def _git_owner() -> str | None:
    """Repository owner from the origin remote, or ``None`` -- resolved at
    runtime so this durable test carries no personal-identity literal.
    """
    try:
        url = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    match = re.search(r"[:/]([^/]+)/[^/]+?(?:\.git)?$", url)
    return match.group(1) if match else None


def _run(env: dict[str, str], *args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Run the harness under ``bash`` with an isolated environment.

    Only ``PATH``/``HOME`` are inherited so a real ``SAGE_*``/``AUTH_TOKEN`` in
    the developer's shell cannot leak into a behavioral scenario.
    """
    base = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    base.update(env)
    return subprocess.run(
        [_BASH or "bash", str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        env=base,
        timeout=timeout,
    )


def _verdicts(stdout: str) -> dict[str, str]:
    """Parse the pass/fail matrix into ``{check_id: PASS|FAIL|SKIP}``."""
    out: dict[str, str] = {}
    for line in stdout.splitlines():
        m = re.match(r"^\s*(PASS|FAIL|SKIP)\s+(\w+)\b", line)
        if m:
            out[m.group(2)] = m.group(1)
    return out


def _detail(stdout: str, check_id: str) -> str:
    """Return the detail text the matrix printed for ``check_id`` -- the text
    after the STATUS and id columns -- or "" if the check produced no line.

    ``_verdicts`` reports only the PASS/FAIL/SKIP token; this lets a test assert
    on the *message* a specific check emitted (e.g. the decoded curl reason a
    connection-level 000 leaves on its FAIL line).
    """
    for line in stdout.splitlines():
        m = re.match(r"^\s*(?:PASS|FAIL|SKIP)\s+(\w+)\s+(.*)$", line)
        if m and m.group(1) == check_id:
            return m.group(2).strip()
    return ""


def _dry_run_rows(stdout: str) -> dict[str, tuple[str, str]]:
    """Parse ``CHECK <id> | prediction: <p> | control: <c>`` lines."""
    out: dict[str, tuple[str, str]] = {}
    for line in stdout.splitlines():
        m = re.match(r"^CHECK\s+(\w+)\s*\|\s*prediction:\s*(.+?)\s*\|\s*control:\s*(.+?)\s*$", line)
        if m:
            out[m.group(1)] = (m.group(2), m.group(3))
    return out


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


#: A responder maps (method, path, body) -> (status, body_text, headers).
Responder = Callable[[str, str, bytes], "tuple[int, str, dict[str, str]]"]


@contextmanager
def serve(responder: Responder) -> Iterator[str]:
    """Run a threaded stub HTTP server; yield its ``http://127.0.0.1:<port>``.

    Response bodies and header values may carry a ``{{BASE_URL}}`` token,
    substituted with the stub's own ``http://127.0.0.1:<port>`` before sending —
    the same shape as an APIM named value. This lets a responder advertise the
    server's own identity (which the host-coherence checks compare against
    ``SAGE_BASE_URL``, in discovery bodies and WWW-Authenticate challenges
    alike) without knowing the ephemeral port at definition time.
    """
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    class _Handler(http.server.BaseHTTPRequestHandler):
        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            # Responders are (method, path, body); a header-sensitive responder may
            # opt into a 4th `accept` parameter (the request's Accept header) so the
            # Accept-routed checks (the browser redirect) can be exercised offline.
            # Existing 3-arg responders are called unchanged.
            if len(inspect.signature(responder).parameters) >= 4:
                status, text, headers = responder(
                    method, self.path, body, self.headers.get("Accept", "")
                )
            else:
                status, text, headers = responder(method, self.path, body)
            text = text.replace("{{BASE_URL}}", base_url)
            payload = text.encode("utf-8")
            self.send_response(status)
            for key, value in headers.items():
                # The {{BASE_URL}} seam applies to header values too — the
                # WWW-Authenticate challenge carries the metadata URL.
                self.send_header(key, value.replace("{{BASE_URL}}", base_url))
            if "Content-Type" not in headers:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_OPTIONS(self) -> None:  # noqa: N802 (CORS preflight)
            self._dispatch("OPTIONS")

        def log_message(self, *_args: object) -> None:  # silence the stub
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _write_stub_cmd(tmp_path: Path, name: str, body: str) -> str:
    """Write an executable bash stub command and return its path."""
    path = tmp_path / name
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _write_chain_probe_stub(tmp_path: Path, output: str) -> str:
    """A TLS-chain-probe seam printing a fixed ``"<cert_count> <verify_code>"``
    line -- standing in for ``openssl s_client -showcerts`` against a live host,
    so the chain-completeness check can be exercised offline.
    """
    return _write_stub_cmd(tmp_path, "chainprobe", f'echo "{output}"\n')


def _write_probe_stub(tmp_path: Path, verdict: str, exit_code: int) -> str:
    """An MCP-probe seam echoing a fixed verdict line and exiting ``exit_code`` --
    standing in for ``mcp_preflight_probe.py`` against a live MCP transport, so the
    bash check's control logic (discovery-200 + unauth-401) is exercised offline.
    The probe's own protocol is proven separately in test_mcp_preflight_probe.py.
    """
    return _write_stub_cmd(tmp_path, "mcpprobe", f'echo "{verdict}"\nexit {exit_code}\n')


def _base_env(stub_url: str, **overrides: str) -> dict[str, str]:
    """A minimal HTTP-layer environment pointed at a stub server."""
    env = {
        "SAGE_FQDN": "sage.test.invalid",
        "CAS_FQDN": "cas.test.invalid",
        "BASE_DOMAIN": "test.invalid",
        "AUTH_TOKEN": "test-token",
        "SAGE_BASE_URL": stub_url,
        "CAS_BASE_URL": stub_url,
        "PREFLIGHT_EXPECTED_VAULTS": "cas",
        "PREFLIGHT_VAULT_SOURCE": "document_store",
    }
    env.update(overrides)
    return env


_NEEDS_BASH = pytest.mark.skipif(_BASH is None, reason="bash not on PATH")
_NEEDS_RUNTIME = pytest.mark.skipif(
    _BASH is None or _CURL is None, reason="behavioral gate needs bash + curl on PATH"
)


# --------------------------------------------------------------------------- #
# A. Detector control-tests (prove the gates themselves can fail)             #
# --------------------------------------------------------------------------- #
def test_guid_detector_fires() -> None:
    """The no-hardcoded-identity scan is only meaningful if its detector fires."""
    assert _GUID_RE.search("00000000-1111-2222-3333-444444444444")
    assert not _GUID_RE.search("api://sage.example  not-a-guid  1234")


# --------------------------------------------------------------------------- #
# B. Structural gate (read the script text -- no external dependencies)       #
# --------------------------------------------------------------------------- #
def test_exists_and_executable() -> None:
    assert _SCRIPT.is_file(), "deploy/cloud-preflight.sh is missing"
    assert os.access(_SCRIPT, os.X_OK), "deploy/cloud-preflight.sh is not executable (chmod +x)"


def test_shebang_and_strict_mode() -> None:
    text = _script_text()
    assert text.startswith("#!/usr/bin/env bash"), "missing bash shebang"
    assert "set -euo pipefail" in text, "missing strict mode (set -euo pipefail)"


@_NEEDS_BASH
def test_bash_syntax_valid() -> None:
    proc = subprocess.run([_BASH or "bash", "-n", str(_SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, f"bash -n reported a syntax error:\n{proc.stderr}"


def test_no_hardcoded_identity() -> None:
    """No tenant GUID, repo owner, or unfilled placeholder is baked in; the
    harness is driven by parameters. Azure-platform suffixes
    (``azure-api.net``/``azurecontainerapps.io``) are constants the script
    *compares against* and are allowed.
    """
    text = _script_text()
    assert not _GUID_RE.search(text), "hardcoded GUID; pass it as a parameter"
    assert "REPLACE-WITH" not in text, "an unfilled deploy placeholder leaked into the harness"
    owner = _git_owner()
    if owner:
        assert owner.lower() not in text.lower(), "hardcoded repository owner; keep identity out"
    # Positive: endpoints are env-driven, not literal.
    assert "SAGE_FQDN" in text and "AUTH_TOKEN" in text, "endpoints/token must be parameterized"


def test_reports_not_fixes() -> None:
    """A verification artifact, never a deploy step: no infra mutation and no
    SAGE write call. The read-only ``discover`` POST is allowed.
    """
    text = _script_text()
    assert not re.search(r"\baz\s+\S+\s+(?:create|update|delete|deploy|set|add|remove)\b", text), (
        "mutating `az` subcommand; the preflight reports, it does not fix"
    )
    assert not re.search(r"-X\s*(?:PUT|DELETE|PATCH)\b", text), "a mutating HTTP verb leaked in"
    assert ":batch" not in text and "/documents" not in text, "a SAGE write endpoint leaked in"


def test_independence_aggregation_pattern() -> None:
    """The 'one failure does not mask others' guarantee is structural: every
    check is dispatched through a single ``run_check`` seam and the exit code is
    computed from an aggregated flag, not from the last check's status.
    """
    text = _script_text()
    assert "set -euo pipefail" in text
    assert "run_check" in text, "no single run_check dispatch seam"
    assert re.search(r"if\s*!\s*run_check", text), "checks are not dispatched via `if ! run_check`"
    assert re.search(r"exit\s+\"?\$\{?(?:fail|failures|exit_code|rc|status)", text), (
        "exit code is not computed from an aggregated failure flag"
    )


# --------------------------------------------------------------------------- #
# C. Inventory gate (--dry-run enumeration -- runtime wiring, not text)       #
# --------------------------------------------------------------------------- #
@_NEEDS_BASH
def test_all_checks_registered() -> None:
    proc = _run({}, "--dry-run")
    assert proc.returncode == 0, f"--dry-run failed:\n{proc.stderr}"
    rows = _dry_run_rows(proc.stdout)
    assert set(rows) == set(_EXPECTED_CHECKS), (
        f"registry drift: extra={set(rows) - _EXPECTED_CHECKS}, "
        f"missing={_EXPECTED_CHECKS - set(rows)}"
    )


@_NEEDS_BASH
def test_every_check_has_prediction_and_control() -> None:
    """Every registered check carries a non-empty prediction AND control --
    proving the anti-coincidental wiring at runtime, not by a source comment.
    """
    proc = _run({}, "--dry-run")
    rows = _dry_run_rows(proc.stdout)
    assert rows, "no enumerable checks"
    for check_id, (prediction, control) in rows.items():
        assert prediction.strip(), f"{check_id}: empty prediction"
        assert control.strip(), f"{check_id}: missing anti-coincidental control"


@_NEEDS_BASH
def test_required_inputs_enforced() -> None:
    """With no endpoint/token the harness fails fast with usage and makes no
    network call (it never reaches a check).
    """
    proc = _run({})  # empty env: no SAGE_FQDN / BASE_DOMAIN / AUTH_TOKEN
    assert proc.returncode != 0, "missing required inputs must fail fast"
    combined = (proc.stdout + proc.stderr).lower()
    assert "usage" in combined or "required" in combined, "no usage/required-input message"
    assert "AUTH_TOKEN" in (proc.stdout + proc.stderr), "usage must name the required token"
    assert not _verdicts(proc.stdout), "no check should run before required inputs are validated"


# --------------------------------------------------------------------------- #
# D. Behavioral gate (stub server + the real script)                          #
# --------------------------------------------------------------------------- #
_DISCOVERY_BODY = (
    '{"resource":"https://sage.test.invalid","authorization_servers":'
    '["https://login.microsoftonline.com/t/v2.0"],"scopes_supported":["Sage.Access"]}'
)
_VAULTS_BODY = '[{"id":"cas","name":"CAS"},{"id":"test","name":"Test"}]'
_HEALTH_BODY = '{"status":"ok","version":"2.0.0"}'
_DISCOVER_OK = '{"mode":"catalog","results":[{"document":{"id":"d1"}}],"total_available":5}'
_LOGIN_BODY = (
    '{"authorization_url":"https://login.microsoftonline.com/t/oauth2/v2.0/authorize'
    "?client_id=abc&redirect_uri=https%3A%2F%2Fcas.test.invalid%2Fapp%2Fauth%2Fcallback"
    '&response_type=code&scope=openid","state":"xyz"}'
)
_HTTP_CHECKS = "edge_discovery,edge_mcp_unauth,edge_authn_backend,liveness,vault_load,retrieval_pg"


def _green(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
    """An all-layers-healthy stub for the SAGE + BFF HTTP surface."""
    p = path.split("?", 1)[0]
    if p == "/.well-known/oauth-protected-resource":
        return 200, _DISCOVERY_BODY, {}
    if p == "/mcp" or p == "/mcp_admin":
        return 401, "", {"WWW-Authenticate": 'Bearer resource_metadata="x"'}
    if p == "/health":
        return 200, _HEALTH_BODY, {}
    if p == "/sage_vaults":
        return 200, _VAULTS_BODY, {}
    if p.endswith("/discover"):
        if b"deterministic" in body:
            return 400, '{"error":"missing_field","detail":"document_id required"}', {}
        return 200, _DISCOVER_OK, {}
    if p == "/app/auth/login":
        return 200, _LOGIN_BODY, {}
    if p == "/app/auth/me":
        return 200, '{"authenticated":false,"user":null}', {}
    return 404, '{"error":"not_found"}', {}


@_NEEDS_RUNTIME
def test_all_green_passes() -> None:
    with serve(_green) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS=_HTTP_CHECKS))
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode == 0, f"healthy tenant must pass:\n{proc.stdout}\n{proc.stderr}"
    assert all(v == "PASS" for v in verdicts.values()), verdicts
    assert set(verdicts) == set(_HTTP_CHECKS.split(",")), verdicts


@_NEEDS_RUNTIME
def test_blanket_404_fails_edge() -> None:
    """THE anti-coincidental test: when every path 404s, the bare /mcp '401'
    look must NOT be credited -- its discovery-200 control failed.
    """

    def blanket(_m: str, _p: str, _b: bytes) -> tuple[int, str, dict[str, str]]:
        return 404, '{"error":"not_found"}', {}

    with serve(blanket) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_discovery,edge_mcp_unauth"))
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode != 0, "a dead edge must fail the run"
    assert verdicts.get("edge_discovery") == "FAIL", verdicts
    assert verdicts.get("edge_mcp_unauth") == "FAIL", (
        "a 404 (not 401) with a failed discovery control must not pass as auth-gating"
    )


@_NEEDS_RUNTIME
def test_discovery_broken_but_mcp_401_fails_edge() -> None:
    """THE discriminating anti-coincidental test: /mcp answers 401 (the
    'as-predicted' look) while the discovery doc is broken (404). A naive check
    that credits the bare 401 would PASS on a dead edge; the discovery-200
    control must reject it.
    """

    def discovery_broken(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        p = path.split("?", 1)[0]
        if p == "/.well-known/oauth-protected-resource":
            return 404, '{"error":"not_found"}', {}
        if p == "/mcp":
            return 401, "", {"WWW-Authenticate": 'Bearer resource_metadata="x"'}
        return _green(method, path, body)

    with serve(discovery_broken) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_discovery,edge_mcp_unauth"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_discovery") == "FAIL", verdicts
    assert verdicts.get("edge_mcp_unauth") == "FAIL", (
        "a 401 must NOT be credited when the discovery-200 control failed -- "
        f"this is the blanket-edge coincidental-pass trap: {verdicts}"
    )
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_mcp_open_fails_edge() -> None:
    """Discovery live but /mcp answers 200 (auth not enforced) -> edge FAIL."""

    def mcp_open(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0] == "/mcp":
            return 200, '{"oops":"unauthenticated reached backend"}', {}
        return _green(method, path, body)

    with serve(mcp_open) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_discovery,edge_mcp_unauth"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_discovery") == "PASS", verdicts
    assert verdicts.get("edge_mcp_unauth") == "FAIL", "an open /mcp must fail the auth-gating check"
    assert proc.returncode != 0


def _redirect_stub(
    browser_status: int, machine_status: int, discovery_status: int = 200
) -> Callable[[str, str, bytes, str], "tuple[int, str, dict[str, str]]"]:
    """A 4-arg (Accept-sensitive) stub for the browser-redirect check: /mcp answers
    ``browser_status`` for an ``Accept: text/html`` request and ``machine_status``
    otherwise; the discovery doc answers ``discovery_status`` (200 = edge live).
    """

    def stub(_method: str, path: str, _body: bytes, accept: str) -> tuple[int, str, dict[str, str]]:
        p = path.split("?", 1)[0]
        if p == "/.well-known/oauth-protected-resource":
            return discovery_status, (_DISCOVERY_BODY if discovery_status == 200 else "{}"), {}
        if p == "/mcp":
            if "text/html" in accept:
                return browser_status, "", {"Location": "https://cas.test.invalid/"}
            return machine_status, "", {"WWW-Authenticate": 'Bearer resource_metadata="x"'}
        return 404, '{"error":"not_found"}', {}

    return stub


@_NEEDS_RUNTIME
def test_browser_redirect_passes() -> None:
    """Browser (Accept: text/html) -> 302 while the machine Accept still 401s, edge
    live -> edge_browser_redirect PASS. The deploy-verified half of criterion #5.
    """
    with serve(_redirect_stub(browser_status=302, machine_status=401)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_browser_redirect"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_browser_redirect") == "PASS", (proc.stdout, proc.stderr)
    assert proc.returncode == 0


@_NEEDS_RUNTIME
def test_browser_redirect_no_302_fails() -> None:
    """THE criterion-#5 regression: the policy applies cleanly but the Accept-match
    silently never fires, so a browser still gets 401. The check must FAIL -- not
    pass on the healthy-looking 401 (the &quot; round-trip silently broke).
    """
    with serve(_redirect_stub(browser_status=401, machine_status=401)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_browser_redirect"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_browser_redirect") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_browser_redirect_blanket_302_fails() -> None:
    """A blanket 302 (every Accept redirected, machine clients included) must FAIL --
    it would break the MCP OAuth handshake. Proves the check discriminates by Accept,
    not merely that *some* 302 appears.
    """
    with serve(_redirect_stub(browser_status=302, machine_status=302)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_browser_redirect"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_browser_redirect") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_browser_redirect_dead_edge_fails() -> None:
    """Discovery doc broken (404): a browser 302 must NOT be credited -- the
    discovery-200 control rejects it (the blanket-edge coincidental-pass trap).
    """
    with serve(_redirect_stub(browser_status=302, machine_status=401, discovery_status=404)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_browser_redirect"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_browser_redirect") == "FAIL", verdicts
    assert proc.returncode != 0


def _cors_stub(
    preflight_status: int, with_cors_headers: bool = True, discovery_status: int = 200
) -> Callable[[str, str, bytes], "tuple[int, str, dict[str, str]]"]:
    """A stub for the CORS-preflight check: a preflight ``OPTIONS`` (to any path)
    answers ``preflight_status`` and, when ``with_cors_headers``, carries the
    ``Access-Control-Allow-*`` headers APIM's ``<cors>`` policy emits; the
    discovery doc answers ``discovery_status`` (200 = edge live).
    """

    def stub(method: str, path: str, _body: bytes) -> tuple[int, str, dict[str, str]]:
        p = path.split("?", 1)[0]
        if p == "/.well-known/oauth-protected-resource":
            return discovery_status, (_DISCOVERY_BODY if discovery_status == 200 else "{}"), {}
        if method == "OPTIONS":
            headers: dict[str, str] = {}
            if with_cors_headers:
                headers = {
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
                    "Access-Control-Allow-Headers": "Authorization,Content-Type",
                }
            return preflight_status, "", headers
        return 404, '{"error":"not_found"}', {}

    return stub


@_NEEDS_RUNTIME
def test_cors_preflight_passes() -> None:
    """Preflight OPTIONS answers 2xx with Access-Control-Allow-* and the edge is
    live -> edge_cors_preflight PASS. The deploy-verified source of criterion #6.
    """
    with serve(_cors_stub(preflight_status=200, with_cors_headers=True)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_cors_preflight"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_cors_preflight") == "PASS", (proc.stdout, proc.stderr)
    assert proc.returncode == 0


@_NEEDS_RUNTIME
def test_cors_preflight_401_fails() -> None:
    """THE preflight-gate regression: validate-jwt 401s the preflight OPTIONS and emits no
    CORS headers, so the browser blocks the call. The check must FAIL -- catching
    the exact production defect this ticket fixes, not passing on a bare status.
    """
    with serve(_cors_stub(preflight_status=401, with_cors_headers=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_cors_preflight"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_cors_preflight") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_cors_preflight_missing_headers_fails() -> None:
    """A 2xx preflight that carries NO Access-Control-Allow-Origin must FAIL -- a
    browser still blocks the call. Proves the check asserts on the CORS header,
    not merely on a 2xx status (the coincidental-pass trap this criterion guards).
    """
    with serve(_cors_stub(preflight_status=200, with_cors_headers=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_cors_preflight"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_cors_preflight") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_cors_preflight_dead_edge_fails() -> None:
    """Discovery doc broken (404): a CORS-carrying preflight must NOT be credited
    -- the discovery-200 control rejects it (the blanket-edge coincidental-pass trap).
    """
    with serve(
        _cors_stub(preflight_status=200, with_cors_headers=True, discovery_status=404)
    ) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_cors_preflight"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_cors_preflight") == "FAIL", verdicts
    assert proc.returncode != 0


def _dcr_stub(
    with_redirect_uris: bool = True,
    qualified_scope: bool = True,
    status: int = 201,
    discovery_status: int = 200,
) -> Callable[[str, str, bytes], "tuple[int, str, dict[str, str]]"]:
    """A stub for the DCR-registration check: ``POST /register`` answers ``status``
    with a body that (optionally) carries a non-empty ``redirect_uris`` array and
    a (optionally) resource-qualified ``scope``; the discovery doc answers
    ``discovery_status`` (200 = edge live). Toggling either field off reproduces a
    production regression the check must catch.
    """

    def stub(method: str, path: str, _body: bytes) -> tuple[int, str, dict[str, str]]:
        p = path.split("?", 1)[0]
        if p == "/.well-known/oauth-protected-resource":
            return discovery_status, (_DISCOVERY_BODY if discovery_status == 200 else "{}"), {}
        if method == "POST" and p == "/register":
            fields = ['"client_id": "NV"', '"token_endpoint_auth_method": "none"']
            if with_redirect_uris:
                fields.append('"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]')
            scope = (
                "api://sage-app-id/Sage.Access offline_access" if qualified_scope else "Sage.Access"
            )
            fields.append(f'"scope": "{scope}"')
            return status, "{" + ", ".join(fields) + "}", {}
        return 404, '{"error":"not_found"}', {}

    return stub


@_NEEDS_RUNTIME
def test_dcr_registration_passes() -> None:
    """A /register 201 whose body carries a non-empty redirect_uris array and a
    resource-qualified scope, edge live -> edge_dcr_registration PASS. This is the
    live DCR sign-in leg the earlier edge work left deploy-only-verifiable.
    """
    with serve(_dcr_stub()) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_dcr_registration"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_dcr_registration") == "PASS", (proc.stdout, proc.stderr)
    assert proc.returncode == 0


@_NEEDS_RUNTIME
def test_dcr_registration_missing_redirect_uris_fails() -> None:
    """THE reported regression: /register returns a correct 201 + client_id but no
    redirect_uris, so a standards MCP client's registration-response parse throws
    ("couldn't register") before /authorize. The check must FAIL on this body, not
    pass on the 201.
    """
    with serve(_dcr_stub(with_redirect_uris=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_dcr_registration"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_dcr_registration") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_dcr_registration_bare_scope_fails() -> None:
    """A /register 201 that advertises the bare "Sage.Access" scope must FAIL --
    Entra can't bind the unqualified scope to the SAGE resource and rejects
    /authorize (AADSTS650053). Proves the check asserts on scope qualification,
    not merely on a 2xx + redirect_uris.
    """
    with serve(_dcr_stub(qualified_scope=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_dcr_registration"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_dcr_registration") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_dcr_registration_dead_edge_fails() -> None:
    """Discovery doc broken (404): a well-formed /register body must NOT be
    credited -- the discovery-200 control rejects it (the blanket-edge
    coincidental-pass trap).
    """
    with serve(_dcr_stub(discovery_status=404)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_dcr_registration"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_dcr_registration") == "FAIL", verdicts
    assert proc.returncode != 0


def _resource_identity_stub(
    resource_matches: bool = True,
    scope_matches: bool = True,
    discovery_status: int = 200,
) -> Callable[[str, str, bytes], "tuple[int, str, dict[str, str]]"]:
    """A stub for the advertised-identity check. When ``resource_matches`` /
    ``scope_matches``, the discovery and /register bodies advertise the stub's
    own base URL (via the ``{{BASE_URL}}`` seam); otherwise they advertise a
    foreign internal-gateway host or the api://-form scope prefix — each a
    production regression the check must catch.
    """
    resource = "{{BASE_URL}}" if resource_matches else "https://apim-foreign.azure-api.net"
    scope_prefix = "{{BASE_URL}}" if scope_matches else "api://sage-app-id"

    def stub(method: str, path: str, _body: bytes) -> tuple[int, str, dict[str, str]]:
        p = path.split("?", 1)[0]
        if p == "/.well-known/oauth-protected-resource":
            body = (
                "{"
                f'"resource": "{resource}", '
                f'"authorization_servers": ["{resource}"], '
                f'"scopes_supported": ["{scope_prefix}/Sage.Access", "offline_access"], '
                '"bearer_methods_supported": ["header"]'
                "}"
            )
            return discovery_status, (body if discovery_status == 200 else "{}"), {}
        if method == "POST" and p == "/register":
            body = (
                "{"
                '"client_id": "NV", '
                '"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"], '
                '"token_endpoint_auth_method": "none", '
                f'"scope": "{scope_prefix}/Sage.Access offline_access"'
                "}"
            )
            return 201, body, {}
        return 404, '{"error":"not_found"}', {}

    return stub


@_NEEDS_RUNTIME
def test_resource_identity_passes() -> None:
    """Discovery resource, scopes_supported, and the /register scope all carry
    the public base URL -> edge_resource_identity PASS: a standards MCP client
    accepts the RFC 9728 origin match and Entra accepts its RFC 8707 resource
    parameter.
    """
    with serve(_resource_identity_stub()) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_resource_identity"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_resource_identity") == "PASS", (proc.stdout, proc.stderr)
    assert proc.returncode == 0


@_NEEDS_RUNTIME
def test_resource_identity_foreign_resource_fails() -> None:
    """THE internal-gateway regression: the discovery doc advertises the
    *.azure-api.net gateway host instead of the public custom domain. Every
    endpoint answers 200, but a standards client rejects the RFC 9728 origin
    mismatch — the check must FAIL rather than credit the healthy statuses.
    """
    with serve(_resource_identity_stub(resource_matches=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_resource_identity"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_resource_identity") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_resource_identity_api_form_scope_fails() -> None:
    """THE api://-scope regression: the resource is the public host but the
    advertised scope prefix is the api://<app-id> audience URI, which can never
    be consistent with the client's https RFC 8707 resource parameter
    (AADSTS9010010 pre-authentication). The check must FAIL on the scope leg
    even though the resource leg matches.
    """
    with serve(_resource_identity_stub(scope_matches=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_resource_identity"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_resource_identity") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_resource_identity_dead_edge_fails() -> None:
    """Discovery doc broken (404): coherent-looking bodies must NOT be credited
    -- the discovery-200 control rejects them (the blanket-edge
    coincidental-pass trap).
    """
    with serve(_resource_identity_stub(discovery_status=404)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_resource_identity"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_resource_identity") == "FAIL", verdicts
    assert proc.returncode != 0


def _mount_discovery_stub(
    pathful_resource: bool = True,
    pathful_challenge: bool = True,
    discovery_status: int = 200,
) -> Callable[[str, str, bytes], "tuple[int, str, dict[str, str]]"]:
    """A stub for the mount-discovery check. When healthy, a 401 on an MCP
    mount carries a challenge pointing at that mount's path-inserted metadata
    document, and the document advertises the path-carrying mount URI as its
    resource. Toggling ``pathful_resource`` off makes the mount documents
    advertise the bare host (the trailing-slash dead-end regression); toggling
    ``pathful_challenge`` off points every challenge at the root document.
    """

    def stub(method: str, path: str, _body: bytes) -> tuple[int, str, dict[str, str]]:
        p = path.split("?", 1)[0]
        if p == "/.well-known/oauth-protected-resource":
            return discovery_status, (_DISCOVERY_BODY if discovery_status == 200 else "{}"), {}
        for mount in ("/mcp_admin", "/mcp"):  # admin first: /mcp is its prefix
            if p == f"/.well-known/oauth-protected-resource{mount}":
                resource = "{{BASE_URL}}" + (mount if pathful_resource else "")
                body = (
                    "{"
                    f'"resource": "{resource}", '
                    '"authorization_servers": ["{{BASE_URL}}"], '
                    '"scopes_supported": ["{{BASE_URL}}/Sage.Access", "offline_access"], '
                    '"bearer_methods_supported": ["header"]'
                    "}"
                )
                return 200, body, {}
            if p == mount:
                pointer = mount if pathful_challenge else ""
                challenge = (
                    "Bearer resource_metadata="
                    f'"{{{{BASE_URL}}}}/.well-known/oauth-protected-resource{pointer}"'
                )
                return 401, "", {"WWW-Authenticate": challenge}
        return 404, '{"error":"not_found"}', {}

    return stub


@_NEEDS_RUNTIME
def test_mount_discovery_passes() -> None:
    """Each mount 401s with a challenge pointing at its path-inserted metadata
    document, and each document advertises the path-carrying mount URI ->
    edge_mount_discovery PASS.
    """
    with serve(_mount_discovery_stub()) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_mount_discovery"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_mount_discovery") == "PASS", (proc.stdout, proc.stderr)
    assert proc.returncode == 0


@_NEEDS_RUNTIME
def test_mount_discovery_bare_host_resource_fails() -> None:
    """THE trailing-slash dead-end regression: the mount documents 200 but
    advertise the bare host as resource. A client's URL serializer turns that
    into https://host/ -- a form Entra can neither match nor register -- so the
    check must FAIL on the document shape, not credit the healthy statuses.
    """
    with serve(_mount_discovery_stub(pathful_resource=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_mount_discovery"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_mount_discovery") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_mount_discovery_root_challenge_fails() -> None:
    """The challenge on an MCP mount points at the ROOT document instead of the
    mount's path-inserted one: the client then reads the bare-host resource and
    dead-ends. The check must FAIL on the challenge pointer even though the
    mount documents themselves are correct.
    """
    with serve(_mount_discovery_stub(pathful_challenge=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_mount_discovery"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_mount_discovery") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_mount_discovery_dead_edge_fails() -> None:
    """Discovery doc broken (404): coherent-looking challenges and documents
    must NOT be credited -- the discovery-200 control rejects them.
    """
    with serve(_mount_discovery_stub(discovery_status=404)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_mount_discovery"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_mount_discovery") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_one_failure_does_not_mask_others() -> None:
    """Independence: a check that fails in the MIDDLE of the run must not abort
    it -- the checks ordered after the failure still run and report. (A naive
    ``set -e``-abort-on-first-failure would drop everything after ``liveness``.)
    """

    def health_down(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0] == "/health":
            return 500, '{"error":"unavailable"}', {}
        return _green(method, path, body)

    with serve(health_down) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS=_HTTP_CHECKS))
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode != 0
    assert verdicts.get("liveness") == "FAIL", verdicts
    # Every selected check produced a verdict -- the mid-run failure masked none.
    assert set(verdicts) == set(_HTTP_CHECKS.split(",")), f"a check was masked: {verdicts}"
    # The checks ordered AFTER the failing one still ran and passed.
    for check in ("edge_discovery", "vault_load", "retrieval_pg"):
        assert verdicts.get(check) == "PASS", f"{check} should be unaffected: {verdicts}"


@_NEEDS_RUNTIME
def test_retrieval_pg_fails_on_postgres_down() -> None:
    """A Postgres-down /discover 500 fails retrieval_pg specifically."""

    def pg_down(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0].endswith("/discover"):
            return 500, '{"error":"storage_unavailable"}', {}
        return _green(method, path, body)

    with serve(pg_down) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="vault_load,retrieval_pg"))
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode != 0
    assert verdicts.get("vault_load") == "PASS", verdicts
    assert verdicts.get("retrieval_pg") == "FAIL", verdicts


@_NEEDS_RUNTIME
def test_kv_anthropic_skips_when_vault_load_fails() -> None:
    """/health green but /sage_vaults empty == startup aborted: vault_load FAILs,
    and its downstream qualifiers SKIP (not FAIL) so one root cause does not
    over-paint the matrix.
    """

    def empty_vaults(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0] == "/sage_vaults":
            return 200, "[]", {}
        return _green(method, path, body)

    checks = "liveness,vault_load,kv_anthropic,sharepoint_discovery"
    with serve(empty_vaults) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS=checks))
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode != 0, "an empty registry must fail the run via vault_load"
    assert verdicts.get("liveness") == "PASS", verdicts
    assert verdicts.get("vault_load") == "FAIL", verdicts
    assert verdicts.get("kv_anthropic") == "SKIP", "anthropic-key rides vault load, not /health"
    assert verdicts.get("sharepoint_discovery") == "SKIP", verdicts


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


def _diag_env(**overrides: str) -> dict[str, str]:
    """Minimal env that runs main() with no checks selected -- diagnostic only."""
    env = {
        "SAGE_FQDN": "sage.test.invalid",
        "BASE_DOMAIN": "test.invalid",
        "AUTH_TOKEN": "test-token",
        # Select a non-existent check id: main() emits the diagnostic, then the
        # check loop matches nothing -> no network, exit 0.
        "PREFLIGHT_CHECKS": "__none__",
    }
    env.update(overrides)
    return env


@_NEEDS_BASH
def test_diagnostic_decodes_jwt_iss_aud_ver() -> None:
    # A real (synthetic) JWT: the diagnostic reports the *decoded* claim values,
    # which only appear if the payload segment was actually base64url-decoded.
    token = _synthetic_jwt(
        iss="https://login.microsoftonline.com/tid/v2.0", aud=_DIAG_GUID, ver="2.0"
    )
    proc = _run(_diag_env(AUTH_TOKEN=token))
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert f"aud={_DIAG_GUID}" in proc.stderr, proc.stderr
    assert "iss=https://login.microsoftonline.com/tid/v2.0" in proc.stderr, proc.stderr
    assert "ver=2.0" in proc.stderr, proc.stderr


@_NEEDS_BASH
def test_diagnostic_degrades_on_non_jwt_token() -> None:
    # The default AUTH_TOKEN is not a 3-segment JWT; the decoder must degrade
    # gracefully (not crash under `set -euo pipefail`) and the run must complete.
    proc = _run(_diag_env())  # AUTH_TOKEN="test-token"
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "token-claims: unavailable" in proc.stderr, proc.stderr
    assert "=== preflight complete" in proc.stderr, "the run must reach completion"


@_NEEDS_BASH
def test_diagnostic_never_prints_raw_token() -> None:
    token = _synthetic_jwt(
        iss="https://login.microsoftonline.com/tid/v2.0", aud=_DIAG_GUID, ver="2.0"
    )
    proc = _run(_diag_env(AUTH_TOKEN=token))
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
# E. MCP-surface checks (mcp_admin auth enforcement + mcp_roundtrip)           #
#                                                                              #
# The bash check owns the anti-coincidental CONTROL logic (discovery-200 edge- #
# live + the unauth-401 gate); the round-trip PROTOCOL is stubbed here via the #
# PREFLIGHT_MCP_PROBE_CMD seam and proven for real in test_mcp_preflight_probe. #
# --------------------------------------------------------------------------- #
def _discovery_broken(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
    """``_green`` but the OAuth discovery doc 404s -- the dead-edge control trap."""
    if path.split("?", 1)[0] == "/.well-known/oauth-protected-resource":
        return 404, '{"error":"not_found"}', {}
    return _green(method, path, body)


@_NEEDS_RUNTIME
def test_mcp_admin_passes_when_401_unauth_and_authed_probe_ok(tmp_path: Path) -> None:
    """Healthy maintenance mount: 401 unauth + an authenticated handshake that
    succeeds, with the discovery-200 control held -> PASS.
    """
    probe = _write_probe_stub(tmp_path, "mode=handshake mount=/mcp_admin initialize=ok", 0)
    with serve(_green) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="mcp_admin", PREFLIGHT_MCP_PROBE_CMD=probe))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("mcp_admin") == "PASS", proc.stdout
    assert proc.returncode == 0


@_NEEDS_RUNTIME
def test_mcp_admin_fails_when_discovery_broken_but_mcp_admin_401(tmp_path: Path) -> None:
    """THE blanket-edge trap for the maintenance mount: /mcp_admin answers 401 and
    the authed probe succeeds, but the discovery-200 control failed (404) -- the
    401 must NOT be credited on a dead edge.
    """
    probe = _write_probe_stub(tmp_path, "mode=handshake mount=/mcp_admin initialize=ok", 0)
    with serve(_discovery_broken) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="mcp_admin", PREFLIGHT_MCP_PROBE_CMD=probe))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("mcp_admin") == "FAIL", (
        f"a 401 must not be credited when the discovery-200 control failed: {proc.stdout}"
    )
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_mcp_admin_fails_when_unauth_returns_200(tmp_path: Path) -> None:
    """Auth not enforced on the maintenance mount: /mcp_admin answers 200 unauth.
    Even with a passing authed probe, the missing 401 must FAIL the check.
    """
    probe = _write_probe_stub(tmp_path, "mode=handshake mount=/mcp_admin initialize=ok", 0)

    def admin_open(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0] == "/mcp_admin":
            return 200, '{"oops":"unauthenticated reached maintenance"}', {}
        return _green(method, path, body)

    with serve(admin_open) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="mcp_admin", PREFLIGHT_MCP_PROBE_CMD=probe))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("mcp_admin") == "FAIL", "an open /mcp_admin must fail auth-enforcement"
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_mcp_admin_fails_when_authed_probe_fails(tmp_path: Path) -> None:
    """401 unauth + discovery-200 both hold, but the authenticated maintenance
    handshake fails (probe exit 1) -> FAIL. A check that asserted only the unauth
    401 would coincidentally pass here.
    """
    probe = _write_probe_stub(tmp_path, "mode=handshake mount=/mcp_admin initialize=fail", 1)
    with serve(_green) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="mcp_admin", PREFLIGHT_MCP_PROBE_CMD=probe))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("mcp_admin") == "FAIL", proc.stdout
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
