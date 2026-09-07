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
import glob
import http.server
import inspect
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import pytest
import yaml

from tests.deploy._stub_server import serve_threaded

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_SCRIPT: Final[Path] = _REPO_ROOT / "deploy" / "cloud-preflight.sh"
#: The sibling helper the resource-registration check's token-probe seam points
#: at in CI: asks the authorization server for a token scoped to one advertised
#: resource, proving (by refusal) whether its identifier URI is registered.
_PROBE_SCRIPT: Final[Path] = _REPO_ROOT / "deploy" / "resource-token-probe.sh"
#: The committed Core API document, plus the vault-config schema behind the one
#: sweep endpoint that returns an untyped dict. Together they are the authority
#: the sweep's shape markers resolve against (CAS-ADR-008, CAS-ADR-042). The
#: OpenAPI document is itself gated against the live FastAPI app, so a marker
#: bound to it is bound transitively to the response model the app serves --
#: which a marker mirrored between the script and a stub is not.
_OPENAPI_SPEC: Final[Path] = _REPO_ROOT / "docs" / "fs" / "sage" / "sage_core_api.openapi.yaml"
_VAULT_CONFIG_SCHEMA: Final[Path] = _REPO_ROOT / "docs" / "fs" / "sage" / "vault_config.schema.json"

_BASH: Final[str | None] = shutil.which("bash")
_CURL: Final[str | None] = shutil.which("curl")


def _bash_inventory(candidates: Iterable[str | None] | None = None) -> tuple[tuple[str, str], ...]:
    """Every distinct ``bash`` among ``candidates`` as ``((version_label, path), ...)``.

    The harness declares a floor of bash 3.2 -- the stock macOS interpreter, and
    the oldest one its constructs must survive. That floor is only exercised if
    the suite actually runs under it, and ``which bash`` alone will not find it
    wherever a newer bash precedes ``/bin/bash`` on PATH. So both candidates are
    resolved and deduplicated by real path: a developer machine typically yields
    the 3.2 floor, CI yields a 5.x, and a machine with both runs the scenarios
    twice.

    A candidate that cannot report its own version is dropped rather than
    guessed at, so a label is always a version the interpreter itself stated.

    ``candidates`` defaults to this host's two interpreter locations, and is a
    parameter because the behaviours worth holding -- two distinct interpreters
    both survive, one reached by two names is kept once, an unparseable version
    is dropped -- are properties of this resolution rather than of whichever
    interpreters the running host happens to have. A host with a single bash can
    express none of them, so asserting only against the default would leave the
    resolution unverified on exactly the machines the suite normally runs on.
    """
    if candidates is None:
        candidates = (shutil.which("bash"), "/bin/bash")
    seen: set[Path] = set()
    found: list[tuple[str, str]] = []
    for candidate in candidates:
        if candidate is None:
            continue
        path = Path(candidate)
        if not path.is_file():
            continue
        real = path.resolve()
        if real in seen:
            continue
        seen.add(real)
        try:
            version = subprocess.run(
                [str(path), "-c", "printf %s ${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if re.fullmatch(r"\d+\.\d+", version):
            found.append((version, str(path)))
    return tuple(found)


#: The interpreters the version-sensitive scenarios run under. Kept as
#: ``(label, path)`` pairs so a parametrized case is named by the bash version it
#: proves rather than by a filesystem path that says nothing about the floor.
_BASH_BINS: Final[tuple[tuple[str, str], ...]] = _bash_inventory()

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
        "edge_advertises_offline_access",
        "edge_advertises_grant_types",
        "edge_serves_openapi_spec",
        "edge_mount_discovery",
        "edge_advertised_resources_registered",
        "mcp_maint",
        "mcp_admin",
        "mcp_roundtrip",
        "edge_authn_backend",
        "liveness",
        "ocr_capability",
        "transfer_upload_gate",
        "transfer_download_gate",
        "vault_load",
        "retrieval_pg",
        "core_api_vault_reads",
        "core_api_document_reads",
        "core_api_parse_filename",
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


def _ingest_post_lines(text: str) -> list[str]:
    """Lines that POST to the documents collection -- i.e. ingest a document.

    ``/documents`` is both a write route (POST = ingest) and a read route
    (GET ``.../documents/{id}`` and ``/headings``, which the read-only Core API
    sweep exercises). A bare substring ban on the path would forbid the reads
    along with the writes, so the write invariant is stated by verb instead.
    """
    return [
        line.strip() for line in text.splitlines() if "http_post" in line and "/documents" in line
    ]


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


def _run(
    env: dict[str, str],
    *args: str,
    timeout: int = 30,
    bash_bin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the harness under ``bash`` with an isolated environment.

    Only ``PATH``/``HOME`` are inherited so a real ``SAGE_*``/``AUTH_TOKEN`` in
    the developer's shell cannot leak into a behavioral scenario.

    ``bash_bin`` pins the interpreter (default: whichever ``bash`` is on PATH),
    which lets a scenario whose subject is a shell-version-sensitive construct
    run under each interpreter :data:`_BASH_BINS` found rather than under one.
    """
    base = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    base.update(env)
    return subprocess.run(
        [bash_bin or _BASH or "bash", str(_SCRIPT), *args],
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

    def _handler_for(base_url: str) -> type[http.server.BaseHTTPRequestHandler]:
        class _Handler(http.server.BaseHTTPRequestHandler):
            def _dispatch(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                # Responders are (method, path, body); a header-sensitive responder may
                # opt into a 4th `accept` parameter (the request's Accept header) so the
                # Accept-routed checks (the browser redirect) can be exercised offline,
                # and a 5th `origin` parameter (the request's Origin header) so a check
                # that must prove it asked cross-origin can be observed rather than
                # assumed. Existing 3- and 4-arg responders are called unchanged.
                arity = len(inspect.signature(responder).parameters)
                if arity >= 5:
                    status, text, headers = responder(
                        method,
                        self.path,
                        body,
                        self.headers.get("Accept", ""),
                        self.headers.get("Origin", ""),
                    )
                elif arity == 4:
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

        return _Handler

    with serve_threaded(_handler_for) as base_url:
        yield base_url


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
def test_ingest_post_detector_fires() -> None:
    """The write-endpoint ban is stated by verb rather than by path substring, so
    it is only meaningful if it still catches an ingest. Prove both directions:
    a POST to the documents collection is caught, and the read forms the Core API
    sweep depends on are not.
    """
    ingest = '  http_post "$base/documents" \'{"source_path":"x"}\' "$AUTH_TOKEN"'
    assert _ingest_post_lines(ingest), "the ban no longer catches an ingest POST"
    reads = (
        '  http_get "$base/documents/$DOC_FIRST_ID" "$AUTH_TOKEN"\n'
        '  http_get "$base/documents/$DOC_FIRST_ID/headings" "$AUTH_TOKEN"\n'
        '  http_post "$base/traverse" "{}" "$AUTH_TOKEN"'
    )
    assert not _ingest_post_lines(reads), "the ban must not forbid document reads"


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
    SAGE write call. The read-only ``discover`` POST is allowed, and so is the
    transfer-gate probe's PUT: it carries a deliberately-invalid one-time
    token, which the app refuses before any staging I/O, so it cannot mutate
    -- the PUT verb is confined to the dedicated probe helper and asserted
    absent everywhere else.
    """
    text = _script_text()
    assert not re.search(r"\baz\s+\S+\s+(?:create|update|delete|deploy|set|add|remove)\b", text), (
        "mutating `az` subcommand; the preflight reports, it does not fix"
    )
    probe_helper = re.search(r"http_put_transfer_probe\(\) \{ # url token\n(?:.*\n)*?\}", text)
    assert probe_helper, "the transfer probe helper must exist and own the sole PUT"
    outside = text.replace(probe_helper.group(0), "")
    assert not re.search(r"-X\s*(?:PUT|DELETE|PATCH)\b", outside), (
        "a mutating HTTP verb leaked in outside the invalid-token transfer probe"
    )
    assert ":batch" not in text, "a SAGE batch-ingest endpoint leaked in"
    assert not _ingest_post_lines(text), f"an ingest POST leaked in: {_ingest_post_lines(text)}"


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


def test_resource_token_probe_exists_and_executable() -> None:
    assert _PROBE_SCRIPT.exists(), f"missing {_PROBE_SCRIPT}"
    assert os.access(_PROBE_SCRIPT, os.X_OK), f"{_PROBE_SCRIPT} is not executable"


@_NEEDS_BASH
def test_resource_token_probe_bash_syntax_valid() -> None:
    proc = subprocess.run(
        [_BASH or "bash", "-n", str(_PROBE_SCRIPT)], capture_output=True, text=True
    )
    assert proc.returncode == 0, f"bash -n failed:\n{proc.stderr}"


def test_resource_token_probe_mints_v2_scope_and_discards_token() -> None:
    """The probe must ask the v2 *scope* endpoint for the resource it is handed
    (``--scope "$1/.default"``, never the v1 ``--resource`` form, whose
    sts.windows.net issuer the edge rejects), and it must discard the minted
    token: the probe's only signal is its exit status, so an access token must
    never reach stdout, a CI log, or a preflight detail line.
    """
    text = _PROBE_SCRIPT.read_text(encoding="utf-8")
    # Scan command lines only: a comment explaining WHY --resource is wrong must
    # neither trip the ban nor satisfy the mint anchor.
    commands = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    collapsed = re.sub(r"\s+", " ", commands.replace("\\\n", " "))
    match = re.search(r"az account get-access-token[^\n]*", collapsed)
    assert match is not None, "probe must mint via `az account get-access-token`"
    mint = match.group(0)
    assert '--scope "$1/.default"' in mint, (
        f"probe must mint for the handed resource via the v2 scope endpoint; got: {mint!r}"
    )
    assert "--resource " not in mint and not mint.rstrip().endswith("--resource"), (
        f"v1 --resource endpoint is rejected at the edge; got: {mint!r}"
    )
    assert "--output none" in mint, (
        f"the minted token must be discarded (--output none), never printed; got: {mint!r}"
    )
    assert not _GUID_RE.search(text), "no identity GUID may be baked into the probe"


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
_HEALTH_BODY = (
    '{"status":"ok","version":"2.0.0","ocr":{"ocrmypdf":true,"tesseract":true,"ghostscript":true}}'
)
#: The document the sweep's catalog probe resolves. The id carries the real
#: document-id shape (8 hex + "_" + slug) because the sweep greps for that shape
#: rather than for a positional "id" -- a vault id or edge id must not be
#: mistakable for a document id.
_PROBE_DOC_ID = "a1b2c3d4_preflight_probe_document"
#: A well-formed but absent vault id / document id. Both clear their shape
#: validators (``^[a-z0-9][a-z0-9_-]{0,63}$`` and ``^[0-9a-f]{8}_[a-z0-9_]+$``)
#: and so reach the registry/store lookup that answers 404 -- which is what makes
#: them usable as the sweep's anti-coincidental controls.
_ABSENT_VAULT = "preflight-probe-absent"
_ABSENT_DOC = "00000000_preflight_probe_absent"
_DISCOVER_OK = (
    '{"mode":"catalog","results":[{"document":{"id":"' + _PROBE_DOC_ID + '"}}],"total_available":5}'
)
#: Read-only Core API bodies. Each carries the shape field its check asserts, so
#: a canned 200 that merely returns *something* fails.
_STATS_BODY = (
    '{"total_documents":42,"by_lifecycle_status":{"active":42},"by_doc_type":{"adr":42},'
    '"by_source_type":{"markdown":42},"total_edges":7,"by_edge_type":{"references":7},'
    '"staging_edge_count":0,"graph_store_size_bytes":1024,"content_store_size_bytes":2048,'
    '"content_store_row_count":10,"content_store_version_count":1,'
    '"content_store_small_fragment_count":0,"health":"ok"}'
)
#: GET /config returns VaultConfig.model_dump(), so the schema's required
#: top-level sections are always present; edge_inference is the one asserted.
_CONFIG_BODY = (
    '{"vault":{"id":"cas"},"document_types":[],"lifecycle":{},'
    '"metadata_extraction":{},"edge_inference":{"tier_assignments":[]}}'
)
#: A DocumentWithContent carrying every property the schema requires -- the
#: shape the binding gate above resolves this stub against.
_DOCUMENT_BODY = (
    '{"id":"' + _PROBE_DOC_ID + '","title":"Probe","source_type":"markdown",'
    '"source_path":"imports/probe.md","lifecycle_status":"active",'
    '"created_by":"preflight","created_at":"2026-01-01T00:00:00Z",'
    '"last_modified_by":"preflight","updated_at":"2026-01-01T00:00:00Z",'
    '"pipeline_status":"abstraction_complete"}'
)
_HEADINGS_BODY = '{"document_id":"' + _PROBE_DOC_ID + '","title":"Probe","headings":[]}'
_TRAVERSE_BODY = '{"start_id":"' + _PROBE_DOC_ID + '","nodes":[]}'
_PARSE_BODY = (
    '{"title":"Probe","project":null,"version_label":null,'
    '"document_date":null,"doc_type":null,"codes":[]}'
)
_LOGIN_BODY = (
    '{"authorization_url":"https://login.microsoftonline.com/t/oauth2/v2.0/authorize'
    "?client_id=abc&redirect_uri=https%3A%2F%2Fcas.test.invalid%2Fapp%2Fauth%2Fcallback"
    '&response_type=code&scope=openid","state":"xyz"}'
)
#: The schema document a healthy edge publishes without a token: SAGE's own,
#: declaring the bearer scheme a caller needs in order to authenticate, and
#: carrying the authored prose read out of the committed specifications.
_OPENAPI_BODY = (
    '{"openapi":"3.1.0","info":{"title":"SAGE Core API","version":"2.0.0",'
    '"description":"Vault-scoped knowledge infrastructure. Documents are never deleted."},'
    '"paths":{"/sage_vaults":{"get":{}}},'
    '"components":{"securitySchemes":{"entraBearer":{"type":"http","scheme":"bearer"}}}}'
)
_HTTP_CHECKS = (
    "edge_discovery,edge_mcp_unauth,edge_authn_backend,liveness,"
    "ocr_capability,vault_load,retrieval_pg,edge_serves_openapi_spec,"
    "core_api_vault_reads,core_api_document_reads,core_api_parse_filename"
)


def _core_api_green(p: str, body: bytes) -> tuple[int, str, dict[str, str]] | None:
    """The read-only Core API surface a healthy tenant serves, or ``None`` when
    ``p`` is not one of its paths (so ``_green`` falls through to its 404).

    The absent-vault and absent-document sentinels answer 404 here exactly as the
    real app does -- that 404 is what the sweep's controls demand, so a stub that
    blanket-200s cannot credit the checks.
    """
    m = re.match(r"^/sage_vaults/([^/]+)(/.*)?$", p)
    if not m:
        return None
    vault, rest = m.group(1), m.group(2) or ""
    if vault == _ABSENT_VAULT:
        return 404, '{"code":"vault_not_found"}', {}
    if rest == "/stats":
        return 200, _STATS_BODY, {}
    if rest == "/config":
        return 200, _CONFIG_BODY, {}
    if rest == "/pending-metadata":
        return 200, "[]", {}
    if rest == "/staging-edges":
        return 200, "[]", {}
    if rest == "/parse-filename":
        # An unknown source_type is rejected at the request boundary (the field is
        # a SourceType enum), which is the check's request-validation control.
        if b'"source_type"' in body and b'"markdown"' not in body:
            return 422, '{"detail":[{"type":"enum","loc":["body","source_type"]}]}', {}
        return 200, _PARSE_BODY, {}
    if rest == "/traverse":
        if _ABSENT_DOC.encode() in body:
            return 404, '{"code":"document_not_found"}', {}
        return 200, _TRAVERSE_BODY, {}
    dm = re.match(r"^/documents/([^/]+)(/.*)?$", rest)
    if dm:
        document, leaf = dm.group(1), dm.group(2) or ""
        if document == _ABSENT_DOC:
            return 404, '{"code":"document_not_found"}', {}
        if leaf == "":
            return 200, _DOCUMENT_BODY, {}
        if leaf == "/headings":
            return 200, _HEADINGS_BODY, {}
    return None


def _green(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
    """An all-layers-healthy stub for the SAGE + BFF HTTP surface."""
    p = path.split("?", 1)[0]
    if p == "/.well-known/oauth-protected-resource":
        return 200, _DISCOVERY_BODY, {}
    if p in ("/mcp", "/mcp_maint", "/mcp_admin"):
        return 401, "", {"WWW-Authenticate": 'Bearer resource_metadata="x"'}
    if p == "/health":
        return 200, _HEALTH_BODY, {}
    if p == "/openapi.json":
        return 200, _OPENAPI_BODY, {"Access-Control-Allow-Origin": "*"}
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
    core = _core_api_green(p, body)
    if core is not None:
        return core
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
def test_ocr_capability_passes_when_all_true() -> None:
    """/health advertising ocr with all three binaries true -> PASS. This is the
    positive case: the cloud image ships the OCR toolchain and the container
    computed the capability at startup."""
    with serve(_green) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="liveness,ocr_capability"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("ocr_capability") == "PASS", f"{verdicts}\n{proc.stdout}"


@_NEEDS_RUNTIME
def test_ocr_capability_fails_on_stale_image() -> None:
    """THE anti-coincidental control: a stale image predating the ocr field
    returns a healthy 2-field /health (status+version) with NO ocr block. That
    must FAIL -- a blanket 200 must not read as green. This is exactly the
    regression the gate exists to catch (the image that shipped without OCR)."""

    def stale(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0] == "/health":
            return 200, '{"status":"ok","version":"2.0.0"}', {}
        return _green(method, path, body)

    with serve(stale) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="liveness,ocr_capability"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("liveness") == "PASS", "the process is still up"
    assert verdicts.get("ocr_capability") == "FAIL", f"{verdicts}\n{proc.stdout}"


@_NEEDS_RUNTIME
def test_ocr_capability_fails_on_missing_binary() -> None:
    """/health advertises the ocr block but a binary is false (image built with
    the ocr Python extra but without the tesseract apt package) -> FAIL. Proves
    the check reads the per-binary booleans, not merely the block's presence."""

    def missing_tesseract(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0] == "/health":
            return (
                200,
                '{"status":"ok","version":"2.0.0",'
                '"ocr":{"ocrmypdf":true,"tesseract":false,"ghostscript":true}}',
                {},
            )
        return _green(method, path, body)

    with serve(missing_tesseract) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="liveness,ocr_capability"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("ocr_capability") == "FAIL", f"{verdicts}\n{proc.stdout}"
    assert "tesseract" in _detail(proc.stdout, "ocr_capability").lower()


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


def _offline_access_stub(
    prm_advertises: bool = True,
    register_advertises: bool = True,
    discovery_status: int = 200,
) -> Callable[[str, str, bytes], "tuple[int, str, dict[str, str]]"]:
    """A stub for the offline_access-advertisement check. When healthy, the
    protected-resource-metadata ``scopes_supported`` AND the ``/register`` scope
    string both carry the bare OIDC ``offline_access`` token alongside the
    resource-qualified ``Sage.Access`` scope; the discovery doc answers
    ``discovery_status`` (200 = edge live). Toggling ``prm_advertises`` /
    ``register_advertises`` off drops offline_access from one leg while the other
    stays healthy — the dropped-advertisement regression the check must catch.
    """
    prm_scopes = (
        '["{{BASE_URL}}/Sage.Access"' + (', "offline_access"' if prm_advertises else "") + "]"
    )
    reg_scope = "{{BASE_URL}}/Sage.Access" + (" offline_access" if register_advertises else "")

    def stub(method: str, path: str, _body: bytes) -> tuple[int, str, dict[str, str]]:
        p = path.split("?", 1)[0]
        if p == "/.well-known/oauth-protected-resource":
            body = (
                "{"
                '"resource": "{{BASE_URL}}", '
                '"authorization_servers": ["{{BASE_URL}}"], '
                f'"scopes_supported": {prm_scopes}, '
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
                f'"scope": "{reg_scope}"'
                "}"
            )
            return 201, body, {}
        return 404, '{"error":"not_found"}', {}

    return stub


@_NEEDS_RUNTIME
def test_advertises_offline_access_passes() -> None:
    """scopes_supported AND the /register scope both carry offline_access, edge
    live -> edge_advertises_offline_access PASS: a standards MCP client requests
    the refresh-token scope and Entra issues a refresh token.
    """
    with serve(_offline_access_stub()) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_advertises_offline_access"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_advertises_offline_access") == "PASS", (proc.stdout, proc.stderr)
    assert proc.returncode == 0


@_NEEDS_RUNTIME
def test_advertises_offline_access_missing_from_metadata_fails() -> None:
    """THE dropped-advertisement regression on the metadata leg: the discovery
    doc omits offline_access from scopes_supported (the /register scope still
    carries it). The check must FAIL on the metadata leg, proving it asserts the
    actual token in scopes_supported, not merely a 200 + a healthy /register.
    """
    with serve(_offline_access_stub(prm_advertises=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_advertises_offline_access"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_advertises_offline_access") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_advertises_offline_access_missing_from_register_fails() -> None:
    """THE dropped-advertisement regression on the /register leg: the discovery
    scopes_supported still carries offline_access but the /register scope string
    drops it. The check must FAIL on the /register leg, proving it asserts the
    token in both legs independently.
    """
    with serve(_offline_access_stub(register_advertises=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_advertises_offline_access"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_advertises_offline_access") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_advertises_offline_access_dead_edge_fails() -> None:
    """Discovery doc broken (404): fully-advertised bodies must NOT be credited
    -- the discovery-200 control rejects them (the blanket-edge
    coincidental-pass trap).
    """
    with serve(_offline_access_stub(discovery_status=404)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_advertises_offline_access"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_advertises_offline_access") == "FAIL", verdicts
    assert proc.returncode != 0


def _grant_types_stub(
    advertises_authorization_code: bool = True,
    advertises_client_credentials: bool = True,
    discovery_status: int = 200,
) -> Callable[[str, str, bytes], "tuple[int, str, dict[str, str]]"]:
    """A stub for the advertised-grant-set check. When healthy, the
    authorization-server metadata's ``grant_types_supported`` carries BOTH the
    interactive ``authorization_code`` grant and the machine ``client_credentials``
    grant; the protected-resource discovery doc answers ``discovery_status``
    (200 = edge live). Toggling either grant off drops it while the other stays
    healthy -- the dropped-grant regression the check must catch, one grant at a
    time so a check matching the array as one blob cannot survive.
    """
    grants = []
    if advertises_authorization_code:
        grants.append('"authorization_code"')
    if advertises_client_credentials:
        grants.append('"client_credentials"')
    grant_array = "[" + ", ".join(grants) + "]"

    def stub(method: str, path: str, _body: bytes) -> tuple[int, str, dict[str, str]]:
        p = path.split("?", 1)[0]
        if p == "/.well-known/oauth-protected-resource":
            body = (
                "{"
                '"resource": "{{BASE_URL}}", '
                '"authorization_servers": ["{{BASE_URL}}"], '
                '"scopes_supported": ["{{BASE_URL}}/Sage.Access", "offline_access"], '
                '"bearer_methods_supported": ["header"]'
                "}"
            )
            return discovery_status, (body if discovery_status == 200 else "{}"), {}
        if p == "/.well-known/oauth-authorization-server":
            body = (
                "{"
                '"issuer": "{{BASE_URL}}", '
                '"registration_endpoint": "{{BASE_URL}}/register", '
                '"response_types_supported": ["code"], '
                f'"grant_types_supported": {grant_array}, '
                '"code_challenge_methods_supported": ["S256"], '
                '"scopes_supported": ["{{BASE_URL}}/Sage.Access", "offline_access"]'
                "}"
            )
            return 200, body, {}
        return 404, '{"error":"not_found"}', {}

    return stub


@_NEEDS_RUNTIME
def test_advertises_grant_types_passes() -> None:
    """Both grants advertised, edge live -> edge_advertises_grant_types PASS: a
    conformant client reading discovery finds the machine-to-machine
    client_credentials leg alongside the interactive one.
    """
    with serve(_grant_types_stub()) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_advertises_grant_types"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_advertises_grant_types") == "PASS", (proc.stdout, proc.stderr)
    assert proc.returncode == 0


@_NEEDS_RUNTIME
def test_advertises_grant_types_missing_client_credentials_fails() -> None:
    """THE dropped-grant regression this check exists for: the metadata still
    advertises authorization_code (so every other edge check stays green) but
    client_credentials is gone. The check must FAIL, proving it asserts the
    machine grant specifically rather than a healthy-looking 200.
    """
    with serve(_grant_types_stub(advertises_client_credentials=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_advertises_grant_types"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_advertises_grant_types") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_advertises_grant_types_missing_authorization_code_fails() -> None:
    """The mirror regression: client_credentials survives but the interactive
    authorization_code grant is dropped. The check must FAIL, proving the two
    grants are asserted independently -- a single match over the whole array
    would credit this and let the browser flow silently stop being advertised.
    """
    with serve(_grant_types_stub(advertises_authorization_code=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_advertises_grant_types"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_advertises_grant_types") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_advertises_grant_types_dead_edge_fails() -> None:
    """Discovery doc broken (404) while the authorization-server metadata is
    fully healthy: the advertised grant set must NOT be credited -- the
    discovery-200 control rejects it (the blanket-edge coincidental-pass trap).
    """
    with serve(_grant_types_stub(discovery_status=404)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_advertises_grant_types"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_advertises_grant_types") == "FAIL", verdicts
    assert proc.returncode != 0


def _openapi_spec_stub(
    spec_status: int = 200,
    title: str = "SAGE Core API",
    declares_scheme: bool = True,
    carries_prose: bool = True,
    gated_status: int = 401,
    discovery_status: int = 200,
    cors_on_get: bool = True,
    cors_on_options: bool = True,
) -> Callable[[str, str, bytes], "tuple[int, str, dict[str, str]]"]:
    """A stub for the schema-document check.

    When healthy the document is served unauthenticated, is SAGE's own,
    declares the bearer scheme, carries the authored prose, and answers a
    cross-origin GET with Access-Control-Allow-Origin; the gated MCP surface
    still answers 401. Each knob turns off exactly one of those, so a test can
    isolate the regression it targets.

    ``cors_on_get`` and ``cors_on_options`` are separate because the operation
    policy's CORS block has to reach the GET response a browser-context reader
    actually consumes -- the preflight OPTIONS is answered by a different
    operation, so a header present there says nothing about this one.
    """

    def responder(method: str, path: str, body: bytes) -> "tuple[int, str, dict[str, str]]":
        p = path.split("?", 1)[0]
        if p == "/.well-known/oauth-protected-resource":
            return discovery_status, _DISCOVERY_BODY, {}
        if p == "/openapi.json":
            if method == "OPTIONS":
                return 200, "", ({"Access-Control-Allow-Origin": "*"} if cors_on_options else {})
            if spec_status != 200:
                return spec_status, "", {"WWW-Authenticate": 'Bearer resource_metadata="x"'}
            components = (
                '"securitySchemes":{"entraBearer":{"type":"http","scheme":"bearer"}}'
                if declares_scheme
                else '"schemas":{}'
            )
            # The generated skeleton's one-line description, versus the authored
            # block an image carrying the specifications publishes.
            description = (
                "Vault-scoped knowledge infrastructure. Documents are never deleted."
                if carries_prose
                else "Salience-Aware Graph Engine - Core API"
            )
            return (
                200,
                f'{{"openapi":"3.1.0","info":{{"title":"{title}","version":"2.0.0",'
                f'"description":"{description}"}},'
                f'"paths":{{"/sage_vaults":{{"get":{{}}}}}},"components":{{{components}}}}}',
                {"Access-Control-Allow-Origin": "*"} if cors_on_get else {},
            )
        if p in ("/mcp", "/mcp_maint", "/mcp_admin"):
            return gated_status, "", {"WWW-Authenticate": 'Bearer resource_metadata="x"'}
        return 404, '{"error":"not_found"}', {}

    return responder


@_NEEDS_RUNTIME
def test_serves_openapi_spec_passes() -> None:
    """Document 200 unauthenticated, SAGE's own, declaring the bearer scheme,
    with the gate still holding elsewhere -> edge_serves_openapi_spec PASS.
    An external caller can now generate a client without the repository.
    """
    with serve(_openapi_spec_stub()) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_serves_openapi_spec"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_serves_openapi_spec") == "PASS", (proc.stdout, proc.stderr)
    assert proc.returncode == 0


@_NEEDS_RUNTIME
def test_serves_openapi_spec_still_gated_fails() -> None:
    """The document still behind the bearer gate -> FAIL. This is the state the
    change exists to end: a caller needs a token to read the document that says
    how to obtain one. Either leg regressing (the app exemption or the APIM
    operation) lands here.
    """
    with serve(_openapi_spec_stub(spec_status=401)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_serves_openapi_spec"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_serves_openapi_spec") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_serves_openapi_spec_without_security_scheme_fails() -> None:
    """A valid 200 document that declares no bearer scheme -> FAIL.

    An image predating the declaration serves a perfectly well-formed document
    that never mentions authentication, which leaves the caller exactly as
    stuck as a 401 would. The scheme grep misses and the check fails: the
    anti-coincidental control on a blanket 200.
    """
    with serve(_openapi_spec_stub(declares_scheme=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_serves_openapi_spec"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_serves_openapi_spec") == "FAIL", verdicts
    assert "entrabearer" in _detail(proc.stdout, "edge_serves_openapi_spec").lower()


@_NEEDS_RUNTIME
def test_serves_openapi_spec_without_authored_prose_fails() -> None:
    """A 200 document carrying only the generated skeleton -> FAIL.

    The prose is read out of the committed specifications at startup, so an
    image built without them publishes every path with every explanation
    missing. Nothing about the status code, the title, or the security scheme
    distinguishes that document from a healthy one, which is why the check
    greps the body for prose that only the authored block contains.
    """
    with serve(_openapi_spec_stub(carries_prose=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_serves_openapi_spec"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_serves_openapi_spec") == "FAIL", verdicts
    assert "prose" in _detail(proc.stdout, "edge_serves_openapi_spec").lower()


@_NEEDS_RUNTIME
def test_serves_openapi_spec_foreign_document_fails() -> None:
    """A 200 carrying some other service's document -> FAIL. Proves the check
    reads what was served rather than crediting any 200 at that path.
    """
    with serve(_openapi_spec_stub(title="Some Other API")) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_serves_openapi_spec"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_serves_openapi_spec") == "FAIL", verdicts


@_NEEDS_RUNTIME
def test_serves_openapi_spec_collapsed_gate_fails() -> None:
    """The document 200s but so does the gated MCP surface -> FAIL.

    Publishing one document must not widen into an open edge. If validate-jwt
    were lost altogether, every path would 200 and the document's own 200 would
    mean nothing; the gated-surface control rejects that reading.
    """
    with serve(_openapi_spec_stub(gated_status=200)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_serves_openapi_spec"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_serves_openapi_spec") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_serves_openapi_spec_dead_edge_fails() -> None:
    """Discovery doc broken (404) while the document itself is fully healthy:
    the publication must NOT be credited -- the discovery-200 control rejects
    it (the blanket-edge coincidental-pass trap).
    """
    with serve(_openapi_spec_stub(discovery_status=404)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_serves_openapi_spec"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_serves_openapi_spec") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_serves_openapi_spec_without_cors_header_fails() -> None:
    """The document is published, well-formed, and unauthenticated -- but the
    GET response carries no Access-Control-Allow-Origin, so a browser-context
    reader cannot load it. The operation's whole reason for carrying its own
    CORS block is that it omits <base /> and so never runs the API-level one;
    losing that block leaves every other assertion in this check green.
    """
    with serve(_openapi_spec_stub(cors_on_get=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_serves_openapi_spec"))
    detail = _detail(proc.stdout, "edge_serves_openapi_spec")
    assert _verdicts(proc.stdout).get("edge_serves_openapi_spec") == "FAIL", proc.stdout
    assert "Access-Control-Allow-Origin" in detail, detail
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_serves_openapi_spec_cors_on_options_only_fails() -> None:
    """The preflight OPTIONS carries the header and the actual GET does not.

    A browser reads the header off the real response, not off the preflight, and
    the two are answered by different APIM operations -- so a check that settled
    for a preflight header would credit exactly the configuration this one
    exists to reject.
    """
    with serve(_openapi_spec_stub(cors_on_get=False, cors_on_options=True)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_serves_openapi_spec"))
    assert _verdicts(proc.stdout).get("edge_serves_openapi_spec") == "FAIL", proc.stdout


@_NEEDS_RUNTIME
def test_serves_openapi_spec_probes_with_an_origin_header() -> None:
    """The CORS assertion is made against a request that actually carries an
    Origin. A header returned to an origin-less request proves nothing about
    cross-origin behaviour, so the probe has to send one.
    """
    healthy = _openapi_spec_stub()
    seen: list[tuple[str, str, str]] = []

    def recording(
        method: str, path: str, body: bytes, accept: str, origin: str
    ) -> tuple[int, str, dict[str, str]]:
        seen.append((method, path.split("?", 1)[0], origin))
        return healthy(method, path, body)

    with serve(recording) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_serves_openapi_spec"))
    assert _verdicts(proc.stdout).get("edge_serves_openapi_spec") == "PASS", proc.stdout
    assert any(m == "GET" and p == "/openapi.json" and origin for m, p, origin in seen), (
        f"no Origin-bearing GET of the schema document was issued: {seen}"
    )


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
        # Path-specific mounts first: /mcp is a string prefix of both.
        for mount in ("/mcp_maint", "/mcp_admin", "/mcp"):
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


_ARR_CHECK = "edge_advertised_resources_registered"


def _advertised_resources_stub(
    discovery_status: int = 200,
    maint_doc_status: int = 200,
    admin_advertises_maint: bool = False,
    root_resource_suffix: str = "",
) -> Callable[[str, str, bytes], "tuple[int, str, dict[str, str]]"]:
    """A stub for the advertised-resources registration check: the root and the
    three per-mount metadata documents each 200 and advertise a ``{{BASE_URL}}``
    resource (bare for the root, path-carrying for the mounts) -- the four
    identities a real edge steers clients to. ``maint_doc_status`` breaks just
    the /mcp_maint document so the unreadable-advertisement leg can be
    exercised; ``discovery_status`` breaks the root (the blanket-edge control);
    ``admin_advertises_maint`` makes the alias mount's document advertise the
    canonical maintenance resource instead of its own path form, so a check
    that constructs the probe set from the known mount paths -- rather than
    reading it out of the documents -- can be told apart.
    ``root_resource_suffix`` appends to the root document's advertised
    resource, so a scenario can advertise a URI the edge would never mean --
    one carrying a shell metacharacter -- without disturbing the mounts.
    """

    def stub(method: str, path: str, _body: bytes) -> tuple[int, str, dict[str, str]]:
        p = path.split("?", 1)[0]
        if p == "/.well-known/oauth-protected-resource":
            if discovery_status != 200:
                return discovery_status, "{}", {}
            root = "{{BASE_URL}}" + root_resource_suffix
            return 200, f'{{"resource": "{root}", "scopes_supported": ["offline_access"]}}', {}
        for mount in ("/mcp_maint", "/mcp_admin", "/mcp"):
            if p == f"/.well-known/oauth-protected-resource{mount}":
                if mount == "/mcp_maint" and maint_doc_status != 200:
                    return maint_doc_status, '{"error":"not_found"}', {}
                advertised = mount
                if mount == "/mcp_admin" and admin_advertises_maint:
                    advertised = "/mcp_maint"
                body = "{" + f'"resource": "{{{{BASE_URL}}}}{advertised}"' + "}"
                return 200, body, {}
        return 404, '{"error":"not_found"}', {}

    return stub


def _write_resource_probe_stub(tmp_path: Path, refuse_suffix: str = "") -> str:
    """A token-probe seam stub: exits 0 (token minted) for every resource,
    except one whose URI ends in ``refuse_suffix``, which is refused the way
    Entra refuses an unregistered identifier URI -- a verbose AADSTS500011
    diagnostic on stderr and a non-zero exit. Nothing goes to stdout, matching
    the real probe's discarded-token contract.
    """
    body = ""
    if refuse_suffix:
        body += (
            f'case "$1" in\n  *{refuse_suffix})\n'
            '    echo "AADSTS500011: The resource principal named $1 was not found'
            ' in the tenant. TraceID: deadbeef CorrelationID: cafe" >&2\n'
            "    exit 1 ;;\nesac\n"
        )
    body += "exit 0\n"
    return _write_stub_cmd(tmp_path, "resprobe", body)


@_NEEDS_RUNTIME
def test_advertised_resources_registered_passes(tmp_path: Path) -> None:
    """Every advertised resource mints a token scoped ``<resource>/.default``
    -> PASS: each identity the edge steers a client to is registered in the
    directory as an identifier URI.
    """
    probe = _write_resource_probe_stub(tmp_path)
    with serve(_advertised_resources_stub()) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_CHECKS=_ARR_CHECK, PREFLIGHT_RESOURCE_TOKEN_PROBE_CMD=probe)
        )
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get(_ARR_CHECK) == "PASS", (proc.stdout, proc.stderr)
    assert proc.returncode == 0


@_NEEDS_RUNTIME
def test_advertised_resources_registered_unregistered_mount_fails(tmp_path: Path) -> None:
    """THE invalid_target regression: the edge advertises a maintenance-mount
    resource whose identifier URI the directory does not hold. Every document
    200s and every app-scoped probe stays green -- only the mint carrying the
    resource is refused (AADSTS500011) -- so the check must FAIL, naming the
    unregistered URI and the refusal code without echoing the raw diagnostic.
    """
    probe = _write_resource_probe_stub(tmp_path, refuse_suffix="/mcp_maint")
    with serve(_advertised_resources_stub()) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_CHECKS=_ARR_CHECK, PREFLIGHT_RESOURCE_TOKEN_PROBE_CMD=probe)
        )
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get(_ARR_CHECK) == "FAIL", (proc.stdout, proc.stderr)
    assert proc.returncode != 0
    detail = _detail(proc.stdout, _ARR_CHECK)
    assert "/mcp_maint" in detail, f"detail must name the unregistered URI: {detail!r}"
    assert "AADSTS500011" in detail, f"detail must carry the refusal code: {detail!r}"
    # Bounded extraction: the code, never the probe's raw diagnostic line.
    assert "TraceID" not in detail, f"raw probe output leaked into the detail: {detail!r}"


@_NEEDS_RUNTIME
def test_advertised_resources_registered_probes_each_advertised_resource(
    tmp_path: Path,
) -> None:
    """The check must put the discriminating question to the authorization
    server once per distinct advertised resource -- a check that greps the
    documents but never mints, or probes a hardcoded subset, cannot see an
    unregistered identifier URI. The recording stub observes the mint attempts.
    """
    record = tmp_path / "probed.txt"
    probe = _write_stub_cmd(tmp_path, "resprobe", f"printf '%s\\n' \"$1\" >> {record}\nexit 0\n")
    with serve(_advertised_resources_stub()) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_CHECKS=_ARR_CHECK, PREFLIGHT_RESOURCE_TOKEN_PROBE_CMD=probe)
        )
        expected = [url, f"{url}/mcp", f"{url}/mcp_admin", f"{url}/mcp_maint"]
    assert _verdicts(proc.stdout).get(_ARR_CHECK) == "PASS", (proc.stdout, proc.stderr)
    probed = record.read_text(encoding="utf-8").split()
    assert sorted(probed) == sorted(expected), f"probed set != advertised set: {probed}"


@_NEEDS_RUNTIME
def test_advertised_resources_registered_probes_what_documents_advertise(
    tmp_path: Path,
) -> None:
    """The probed set must come from the DOCUMENTS, not from the known mount
    paths: with the alias mount's document advertising the canonical
    maintenance resource, the distinct advertised set is three URIs -- probed
    once each, and the alias path form (which no document advertises) not at
    all. A check that constructs <base>+<mount> for each known mount produces
    the same four-URI set as the healthy case and cannot pass here.
    """
    record = tmp_path / "probed.txt"
    probe = _write_stub_cmd(tmp_path, "resprobe", f"printf '%s\\n' \"$1\" >> {record}\nexit 0\n")
    with serve(_advertised_resources_stub(admin_advertises_maint=True)) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_CHECKS=_ARR_CHECK, PREFLIGHT_RESOURCE_TOKEN_PROBE_CMD=probe)
        )
        expected = [url, f"{url}/mcp", f"{url}/mcp_maint"]
    assert _verdicts(proc.stdout).get(_ARR_CHECK) == "PASS", (proc.stdout, proc.stderr)
    probed = record.read_text(encoding="utf-8").split()
    assert sorted(probed) == sorted(expected), (
        f"probed set must be the documents' distinct advertised set: {probed}"
    )


@_NEEDS_RUNTIME
def test_advertised_resources_registered_probes_the_uri_not_a_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probed URI is the one the document advertised, never one assembled
    from the working directory.

    The accumulated resource set is read back through an *unquoted* expansion,
    which is how the set is split into words -- and pathname expansion applies
    to that same expansion. A resource carrying a glob character is therefore
    replaced by whatever matches it on disk, so the check mints for a URI no
    document advertised and credits (or refuses) the wrong identity.

    A URL is not obviously a glob pattern, which is what makes the defect easy
    to miss, but it is one: the shell reads ``//`` as a single separator and the
    trailing component as a pattern, so ``<base>/mcp?`` matches a file at
    ``./http:/<host>:<port>/mcpX``. The bait is planted here for exactly that
    reason -- without it the pattern matches nothing, stays literal, and the
    scenario would be satisfied by the very expansion it is meant to exclude.

    The advertised suffix is deliberate rather than realistic: the shape has to
    be one the runner's directory can satisfy. What is realistic is the
    surrounding condition, since the harness runs from a checkout and probes
    whatever URIs the tenant happens to advertise.
    """
    record = tmp_path / "probed.txt"
    probe = _write_stub_cmd(tmp_path, "resprobe", f"printf '%s\\n' \"$1\" >> {record}\nexit 0\n")
    with serve(_advertised_resources_stub(root_resource_suffix="/mcp?")) as url:
        scheme, host_port = url.split("://", 1)
        bait = tmp_path / f"{scheme}:" / host_port
        bait.mkdir(parents=True)
        (bait / "mcpX").touch()
        monkeypatch.chdir(tmp_path)
        # Positive control on the bait, as in the vault-load scenario: a bait
        # that does not match leaves this test satisfied by an expanding loop.
        assert glob.glob(f"{scheme}:/{host_port}/mcp?"), (
            "the bait is not a live glob match; the scenario is inert"
        )
        proc = _run(
            _base_env(url, PREFLIGHT_CHECKS=_ARR_CHECK, PREFLIGHT_RESOURCE_TOKEN_PROBE_CMD=probe)
        )
        advertised, expanded = f"{url}/mcp?", f"{url}/mcpX"
    assert _verdicts(proc.stdout).get(_ARR_CHECK) == "PASS", (proc.stdout, proc.stderr)
    probed = record.read_text(encoding="utf-8").split()
    assert advertised in probed, f"the advertised URI was not the one probed: {probed}"
    assert expanded not in probed, f"a filename beside the run reached the probe: {probed}"


@_NEEDS_RUNTIME
def test_advertised_resources_registered_dead_edge_fails(tmp_path: Path) -> None:
    """Discovery doc broken (404): a green token probe must NOT be credited --
    the discovery-200 control rejects the blanket-edge coincidental pass, and
    the detail must say the CONTROL failed (edge not live), not misdiagnose a
    dead edge as one unreadable metadata document.
    """
    probe = _write_resource_probe_stub(tmp_path)
    with serve(_advertised_resources_stub(discovery_status=404)) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_CHECKS=_ARR_CHECK, PREFLIGHT_RESOURCE_TOKEN_PROBE_CMD=probe)
        )
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get(_ARR_CHECK) == "FAIL", verdicts
    assert proc.returncode != 0
    detail = _detail(proc.stdout, _ARR_CHECK)
    assert "control failed" in detail, f"dead edge must be named as the control leg: {detail!r}"


@_NEEDS_RUNTIME
def test_advertised_resources_registered_missing_mount_doc_fails(tmp_path: Path) -> None:
    """One mount's metadata document is unreadable (404): the advertised set
    cannot be fully read, and a check that silently shrank to the readable
    subset would credit exactly the mount most likely to be broken -- so FAIL,
    even though every readable resource mints fine.
    """
    probe = _write_resource_probe_stub(tmp_path)
    with serve(_advertised_resources_stub(maint_doc_status=404)) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_CHECKS=_ARR_CHECK, PREFLIGHT_RESOURCE_TOKEN_PROBE_CMD=probe)
        )
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get(_ARR_CHECK) == "FAIL", verdicts
    assert proc.returncode != 0
    assert "mcp_maint" in _detail(proc.stdout, _ARR_CHECK)


@_NEEDS_RUNTIME
def test_advertised_resources_registered_skips_when_probe_seam_unset() -> None:
    """No token-probe seam (an operator run without an az session): the check
    must SKIP -- neither FAIL (the edge may be perfectly healthy) nor PASS
    (registration was not verified) -- and say which seam arms it.
    """
    with serve(_advertised_resources_stub()) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS=_ARR_CHECK))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get(_ARR_CHECK) == "SKIP", (proc.stdout, proc.stderr)
    assert proc.returncode == 0
    assert "PREFLIGHT_RESOURCE_TOKEN_PROBE_CMD" in _detail(proc.stdout, _ARR_CHECK)


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


# --------------------------------------------------------------------------- #
# D2. The expected-vaults comma list (vault_load's per-id loop)               #
#                                                                             #
# ``check_vault_load`` splits PREFLIGHT_EXPECTED_VAULTS under ``local IFS=','``
# and asserts each id came back from /sage_vaults. A tenant starts life with one
# vault and grows: the deploy variable is operator-edited, and the multi-id path
# is the one an operator reaches first when adding the second vault. These
# scenarios hold the split's seven decided behaviours -- iterate every id, skip a
# null element, name only the absent id, treat surrounding whitespace as part of
# the id, match a whole id rather than a substring of one, compare an id
# literally rather than as a pattern, and take an id as written rather than as a
# pattern over the working directory -- and they run under each interpreter the
# host offers, because the split's semantics are a property of the shell rather
# than of the script.
#
# The stub advertises two vaults (``_VAULTS_BODY``), so a multi-id expectation is
# satisfiable without a bespoke responder.
# --------------------------------------------------------------------------- #
#: One case per inventoried interpreter, named by the version it proves rather
#: than by a filesystem path that says nothing about the floor. A label can repeat
#: -- two installs may report the same major.minor -- so when it does, *every*
#: case sharing it takes a positional suffix (``bash5.2#0``, ``bash5.2#1``), which
#: keeps the ids distinct and keeps the pair visibly a pair. A lone label is left
#: bare, so the ordinary single-interpreter host reads as ``bash3.2``.
_BASH_BIN_PARAMS: Final[list[pytest.param]] = [
    pytest.param(
        path,
        id=f"bash{label}" if [lb for lb, _ in _BASH_BINS].count(label) == 1 else f"bash{label}#{i}",
    )
    for i, (label, path) in enumerate(_BASH_BINS)
]

#: The detail line a satisfied vault_load emits against ``_VAULTS_BODY``.
_VAULT_LOAD_CLEAN_DETAIL: Final[str] = "2 vault(s) loaded; expected id(s) present"


def _green_one_vault(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
    """``_green`` with the second vault withdrawn from the registry.

    A healthy tenant that simply holds fewer vaults than the deploy variable
    expects. Used as the differential against ``_green``: the same multi-id
    expectation must pass on one and fail on the other, which is what shows the
    trailing id is genuinely consulted rather than carried along.
    """
    if path.split("?", 1)[0] == "/sage_vaults":
        return 200, '[{"id":"cas","name":"CAS"}]', {}
    return _green(method, path, body)


def _green_metacharacter_vault(
    method: str, path: str, body: bytes
) -> tuple[int, str, dict[str, str]]:
    """``_green`` with a vault whose id carries a regex metacharacter.

    The only fixture in which an *advertised* id is not slug-shaped, and the
    only one that can separate comparing an id literally from refusing to
    compare a metacharacter-bearing one at all.
    """
    if path.split("?", 1)[0] == "/sage_vaults":
        return 200, '[{"id":"c.s","name":"CAS"},{"id":"test","name":"Test"}]', {}
    return _green(method, path, body)


def test_bash_inventory_is_populated_and_labelled() -> None:
    """The interpreter inventory found something, and labelled it credibly.

    Every scenario below is parametrized over :data:`_BASH_BINS`. An inventory
    that resolved to nothing would collect zero cases from each of them -- a
    green suite covering none of the behaviour it claims -- and pytest reports a
    parametrization over an empty sequence as a skip, not a failure. This is the
    detector control that makes that state loud.
    """
    assert _BASH_BINS, "no bash interpreter resolved; the split scenarios would collect nothing"
    labels = [label for label, _ in _BASH_BINS]
    assert all(re.fullmatch(r"\d+\.\d+", label) for label in labels), labels
    # Uniqueness is asserted on the real path, which is what the resolution
    # deduplicates on. Labels are deliberately not required to be unique: two
    # separate installs can report the same major.minor, and both should run.
    reals = [Path(path).resolve() for _, path in _BASH_BINS]
    assert len(set(reals)) == len(reals), f"an interpreter was inventoried twice: {reals}"
    if _BASH is not None:
        assert Path(_BASH).resolve() in set(reals), "the PATH bash is missing from the inventory"


def _write_version_stub(tmp_path: Path, name: str, version: str) -> str:
    """An interpreter-shaped stub that reports ``version`` and ignores its args.

    Stands in for a second bash at a distinct path. A symlink would not do: the
    resolution deduplicates by real path, so a link to the host's own bash is
    correctly collapsed rather than counted twice.
    """
    return _write_stub_cmd(tmp_path, name, f'printf %s "{version}"\n')


def test_bash_inventory_keeps_distinct_interpreters_and_collapses_aliases(tmp_path: Path) -> None:
    """The resolution counts interpreters by real path and trusts only real versions.

    Held against synthetic candidates rather than against the host's own
    interpreters, because a single-bash host -- this suite's normal case, and
    CI's -- cannot distinguish any of these outcomes from any other. Without this
    the sibling scenario above is the only check on the resolution, and it passes
    unchanged against a resolution that silently drops one interpreter of two,
    which is the one bug that would quietly stop exercising the declared floor.
    """
    first = _write_version_stub(tmp_path, "bash-first", "3.2")
    second = _write_version_stub(tmp_path, "bash-second", "5.2")
    same_as_second = _write_version_stub(tmp_path, "bash-second-again", "5.2")
    unparseable = _write_version_stub(tmp_path, "bash-mute", "not-a-version")

    assert _bash_inventory([first, second]) == (("3.2", first), ("5.2", second)), (
        "two interpreters at distinct real paths must both survive"
    )
    # The case that separates keying on the real path from keying on the version
    # label. Two separate installs reporting the same major.minor are two
    # interpreters and must both run; a label-keyed resolution would silently keep
    # one, and the distinct-version case above cannot tell the two policies apart.
    assert _bash_inventory([second, same_as_second]) == (
        ("5.2", second),
        ("5.2", same_as_second),
    ), "two installs reporting the same version are still two interpreters"
    assert _bash_inventory([first, first]) == (("3.2", first),), (
        "one interpreter named twice must be inventoried once"
    )
    assert _bash_inventory([unparseable]) == (), "a candidate with no parseable version is dropped"
    assert _bash_inventory([None, str(tmp_path / "absent"), second]) == (("5.2", second),), (
        "an unresolved name and a nonexistent path are skipped without losing a real candidate"
    )


@_NEEDS_RUNTIME
@pytest.mark.parametrize("bash_bin", _BASH_BIN_PARAMS)
def test_vault_load_passes_with_every_expected_id_present(bash_bin: str) -> None:
    """Two expected ids, both advertised -> PASS with the clean detail line.

    The happy path the deploy variable reaches as soon as a tenant holds a second
    vault, and the source of the message the scenarios below compare against.

    Asserted as a *differential*, because the pass on its own is not evidence of
    anything: the single-id expectation this suite already carried produces a
    byte-identical PASS and detail line, so a run that consulted only the first
    id would look exactly like this one. Withdrawing the second vault from the
    registry and re-running the same expectation is what makes the trailing id
    load-bearing -- the second arm can only fail if that id was looked up.
    """
    env = {"PREFLIGHT_EXPECTED_VAULTS": "cas,test", "PREFLIGHT_CHECKS": "vault_load"}
    with serve(_green) as url:
        proc = _run(_base_env(url, **env), bash_bin=bash_bin)
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode == 0, f"both ids are present:\n{proc.stdout}\n{proc.stderr}"
    assert verdicts.get("vault_load") == "PASS", verdicts
    assert _detail(proc.stdout, "vault_load") == _VAULT_LOAD_CLEAN_DETAIL, proc.stdout

    with serve(_green_one_vault) as url:
        withdrawn = _run(_base_env(url, **env), bash_bin=bash_bin)
    detail = _detail(withdrawn.stdout, "vault_load")
    assert withdrawn.returncode != 0, (
        f"the same expectation must fail once a vault it names is gone:\n{withdrawn.stdout}"
    )
    assert _verdicts(withdrawn.stdout).get("vault_load") == "FAIL", withdrawn.stdout
    assert "test" in detail, f"the withdrawn id must be named: {detail!r}"


@_NEEDS_RUNTIME
@pytest.mark.parametrize("bash_bin", _BASH_BIN_PARAMS)
@pytest.mark.parametrize("expected", ["cas,,test", ",cas,test"], ids=["interior", "leading"])
def test_vault_load_skips_null_elements_in_the_expected_list(expected: str, bash_bin: str) -> None:
    """A null element in the comma list is skipped, not looked up.

    With IFS holding only a comma, an interior ``,,`` and a leading ``,`` each
    produce a genuine empty field, which the loop's ``[ -z "$v" ] && continue``
    guard drops. Without that guard the empty id is searched for, never found,
    and the run fails while naming nothing -- the guard is what keeps a stray
    comma in an operator-edited variable from failing a healthy deploy.

    A *trailing* comma is deliberately not among these shapes: it yields exactly
    one field, so it never reaches the guard, and including it would read as
    coverage while proving nothing.
    """
    with serve(_green) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_EXPECTED_VAULTS=expected, PREFLIGHT_CHECKS="vault_load"),
            bash_bin=bash_bin,
        )
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode == 0, f"a null element must not fail the gate:\n{proc.stdout}"
    assert verdicts.get("vault_load") == "PASS", verdicts
    assert _detail(proc.stdout, "vault_load") == _VAULT_LOAD_CLEAN_DETAIL, proc.stdout


@_NEEDS_RUNTIME
@pytest.mark.parametrize("bash_bin", _BASH_BIN_PARAMS)
@pytest.mark.parametrize("expected", ["cas,nosuch", "nosuch,cas"], ids=["last", "first"])
def test_vault_load_names_only_the_absent_id(expected: str, bash_bin: str) -> None:
    """One absent id among several fails, and the detail names *that* id.

    Both directions of the message are asserted. That the absent id appears
    proves the report is specific; that the present id does *not* appear proves
    it reports what is missing rather than echoing what was expected -- the
    difference between an operator reading which vault failed to load and
    re-checking the whole list by hand.

    Both *positions* are exercised too, and that is what makes this the
    loop-completeness scenario: an absence in the trailing position is only
    detected by a loop that did not stop early, and one in the leading position
    only by a loop that did not skip its first iteration. Either shape alone
    leaves half the loop unproven.
    """
    with serve(_green) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_EXPECTED_VAULTS=expected, PREFLIGHT_CHECKS="vault_load"),
            bash_bin=bash_bin,
        )
    verdicts = _verdicts(proc.stdout)
    detail = _detail(proc.stdout, "vault_load")
    assert proc.returncode != 0, "an absent expected id must fail the run"
    assert verdicts.get("vault_load") == "FAIL", verdicts
    assert "nosuch" in detail, f"the absent id must be named: {detail!r}"
    assert "cas" not in detail, f"the present id must not be reported missing: {detail!r}"


@_NEEDS_RUNTIME
@pytest.mark.parametrize("bash_bin", _BASH_BIN_PARAMS)
@pytest.mark.parametrize(
    "expected", [" cas , test ", " cas,test "], ids=["both-sides", "opposite-sides"]
)
def test_vault_load_does_not_trim_whitespace_around_ids(expected: str, bash_bin: str) -> None:
    """Whitespace around an id is part of the id, and fails the lookup.

    IFS holds only a comma, so a space is not a delimiter: `` cas `` is a
    distinct id from ``cas`` and its lookup fails even though ``cas`` is
    advertised. This is the decided contract rather than an oversight -- the
    documentation-side half of it is held by
    ``tests/sage/test_cloud_test_vault_seed_config.py``, which splits a
    documented value exactly as this loop does and refuses to trim, on the
    grounds that a check more permissive than the thing it checks would pass a
    value the deploy then rejects. Pinning the behaviour here keeps the two
    halves from drifting apart.

    The two shapes are not redundant, and the second is what gives the scenario
    teeth. ``" cas , test "`` pads both ids on both sides, which is the shape an
    operator actually types -- but a trim that strips only one side leaves each id
    still padded, still missing, and still reported once, so that shape alone is
    satisfied identically by a one-sided trim. ``" cas,test "`` pads the two ids on
    opposite sides, so a leading-only strip rescues ``cas`` while a trailing-only
    strip rescues ``test``: either drops an id out of the missing list and fails
    the assertion below.
    """
    with serve(_green) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_EXPECTED_VAULTS=expected, PREFLIGHT_CHECKS="vault_load"),
            bash_bin=bash_bin,
        )
    verdicts = _verdicts(proc.stdout)
    detail = _detail(proc.stdout, "vault_load")
    assert proc.returncode != 0, "padded ids are not the advertised ids; the gate must fail"
    assert verdicts.get("vault_load") == "FAIL", verdicts
    # Both ids stay missing. Under the opposite-sides shape a trim of either side
    # rescues exactly one of them, so requiring both is what separates no-trim
    # from a partial trim.
    assert detail.count("cas") == 1 and detail.count("test") == 1, detail
    assert "missing expected id(s)" in detail, detail


@_NEEDS_RUNTIME
@pytest.mark.parametrize("bash_bin", _BASH_BIN_PARAMS)
@pytest.mark.parametrize("expected", ["ca", "as", "Test"], ids=["prefix", "suffix", "name"])
def test_vault_load_requires_a_whole_id_not_a_substring(expected: str, bash_bin: str) -> None:
    """Only a whole advertised *id* satisfies the gate.

    Three shapes that must not be credited, each pinning a different part of the
    lookup pattern, and each verified by the mutation that admits it:

    * ``ca``, a prefix of the advertised ``cas`` -- held by the closing quote.
      Dropping it admits the prefix while the other two shapes stay green.
    * ``as``, a suffix of the same id -- held by the left bound as a whole.
      Reducing the pattern to a bare ``$v"`` admits the suffix while the prefix
      stays green.
    * ``Test``, which is no id at all: it is the *name* of the advertised ``test``
      vault. Held by the pattern being keyed on ``"id"``. A rival matching any
      quoted value, ``grep -qE "\\"$v\\""``, passes every other scenario in this
      section and credits this expectation through ``"name":"Test"`` -- so without
      this shape nothing pins that the gate reads ids rather than whatever the
      registry happens to quote.

    One rival is deliberately *not* claimed as excluded by any single shape here.
    Dropping only the opening quote (``…:[[:space:]]*$v"``) leaves a pattern that
    cannot match any JSON string value, since the quote opening the advertised
    value still sits between the colon and the id; it is caught by the happy-path
    and null-element scenarios above rather than by this one.
    """
    with serve(_green) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_EXPECTED_VAULTS=expected, PREFLIGHT_CHECKS="vault_load"),
            bash_bin=bash_bin,
        )
    verdicts = _verdicts(proc.stdout)
    detail = _detail(proc.stdout, "vault_load")
    assert proc.returncode != 0, "a substring must not satisfy the expected-id lookup"
    assert verdicts.get("vault_load") == "FAIL", verdicts
    # The rendered id, not a substring of it: `"ca" in detail` is also satisfied
    # by a run that reported the advertised `cas` missing, which is a different
    # defect entirely and must not be credited as this one.
    assert detail.endswith(f"missing expected id(s): {expected}"), detail


@_NEEDS_RUNTIME
@pytest.mark.parametrize("bash_bin", _BASH_BIN_PARAMS)
@pytest.mark.parametrize(
    "expected",
    ["c.s", ".*", "cas|test", "ca[s]"],
    ids=["dot", "wildcard", "alternation", "bracket"],
)
def test_vault_load_treats_a_metacharacter_id_as_a_literal(expected: str, bash_bin: str) -> None:
    """An expected id is compared as a string, never evaluated as a pattern.

    The lookup has to assert the surrounding JSON shape, and a regular
    expression is how it does so -- but an expected id interpolated into that
    expression is read as pattern syntax too. An id carrying a metacharacter
    then matches an advertised id it is not equal to, and the gate credits a
    vault that never loaded. That is the one direction of error a preflight
    cannot afford: an operator reads a green vault_load as proof the named vault
    is serving.

    None of these shapes is an id anyone means to type; they are what an
    operator-edited list can acquire by accident. Each pins a different escape
    class, so no partial fix passes the set:

    * ``c.s`` -- an unescaped ``.`` spans the advertised ``cas``. Excluded by
      escaping ``.`` alone.
    * ``.*`` -- the same defect at its widest: satisfied by *whatever* loaded,
      so a gate naming a specific vault stops naming one at all. Also excluded
      by escaping ``.``, and carried because it shows the severity rather than
      the mechanism.
    * ``cas|test`` -- alternation, whose left branch matches through the
      advertised ``cas``. Survives an escape covering only ``.`` and ``*``, so
      it separates a partial escape from a literal comparison.
    * ``ca[s]`` -- a bracket expression. Survives an escape covering ``.``,
      ``*`` and ``|``, and is the shape a hand-rolled character-class escape is
      likeliest to mangle.

    Every shape is credited by an unescaped interpolation, so the set is red
    against a pattern-matching lookup and green only once the comparison is
    literal. The scenario is a differential in the same sense as its siblings:
    a lookup that simply failed everything would satisfy it, and what excludes
    that is the happy-path scenario above, which can only stay green if a
    genuine id still resolves.
    """
    with serve(_green) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_EXPECTED_VAULTS=expected, PREFLIGHT_CHECKS="vault_load"),
            bash_bin=bash_bin,
        )
    verdicts = _verdicts(proc.stdout)
    detail = _detail(proc.stdout, "vault_load")
    assert proc.returncode != 0, (
        f"a metacharacter id must not be credited by the ids it patterns over:\n{proc.stdout}"
    )
    assert verdicts.get("vault_load") == "FAIL", verdicts
    # The rendered id, as in the sibling scenario: a bare containment check is
    # also satisfied by a run that reported some *other* id missing, which is a
    # different defect and must not be credited as this one.
    assert detail.endswith(f"missing expected id(s): {expected}"), detail


@_NEEDS_RUNTIME
@pytest.mark.parametrize("bash_bin", _BASH_BIN_PARAMS)
@pytest.mark.parametrize("expected", ["zzz\ncas", "cas\nzzz"], ids=["match-second", "match-first"])
def test_vault_load_does_not_credit_a_multi_line_id_by_one_of_its_lines(
    expected: str, bash_bin: str
) -> None:
    """An id spanning two lines is one id, and is not satisfied by either line.

    The sibling scenario above pins that the comparison is not a *pattern*
    match. This pins the narrower thing a pattern-free comparison can still get
    wrong: a matcher fed the id as a pattern *list* rather than a pattern reads
    a newline as a separator between entries, so a two-line value is credited
    whenever any one of its lines names an advertised vault. The value stops
    being compared and starts being enumerated.

    ``IFS=','`` makes this reachable rather than theoretical -- a newline is not
    a delimiter there, so it stays inside a field -- and the variable is
    supplied by a GitHub Actions repository variable, which admits multi-line
    values. The direction of error is the gate's worst one: `zzz` is credited
    because `cas`, sharing its value, loaded.

    Both orderings are carried because they fail differently under a matcher
    that stops at its first entry: ``cas\\nzzz`` is credited by the leading
    line, ``zzz\\ncas`` only by a matcher that reads on past it. Either alone
    leaves half of the enumeration unproven.
    """
    with serve(_green) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_EXPECTED_VAULTS=expected, PREFLIGHT_CHECKS="vault_load"),
            bash_bin=bash_bin,
        )
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode != 0, (
        f"a multi-line id must not be credited by one of its lines:\n{proc.stdout}"
    )
    assert verdicts.get("vault_load") == "FAIL", verdicts
    # Asserted against the whole of stdout rather than the parsed detail: the id
    # carries a newline, so the missing list it renders into spans two output
    # lines and ``_detail`` is single-line by construction. Both of the id's
    # lines must be reported -- an assertion on ``zzz`` alone is also satisfied
    # by a run that credited ``cas`` and reported only the remainder, which is
    # the defect this scenario exists to exclude.
    assert "missing expected id(s):" in proc.stdout, proc.stdout
    assert "zzz" in proc.stdout and "cas" in proc.stdout, (
        f"both lines of the id must be reported missing:\n{proc.stdout}"
    )


@_NEEDS_RUNTIME
@pytest.mark.parametrize("bash_bin", _BASH_BIN_PARAMS)
def test_vault_load_credits_an_advertised_id_that_carries_a_metacharacter(bash_bin: str) -> None:
    """A metacharacter in an id is not itself disqualifying: an id equal to one
    the registry advertises is satisfied, whatever characters it holds.

    The guard against over-correcting the scenario above. Refusing any id
    carrying a metacharacter would fail every shape that one asserts, and pass
    every other scenario in this section, because no other fixture advertises an
    id that is not slug-shaped -- so the two rules are indistinguishable across
    the whole suite without this case. They part here, on the one input that
    separates them, and they part in the direction that matters: a rule keyed on
    the characters in an id reports a vault absent while it is serving.

    Unlike its siblings this scenario is not red against the pattern-matching
    lookup it replaces -- a regular expression also matches its own literal
    text. It is a guard rather than a detector, and the mutation it answers is
    a comparison narrowed to slug-shaped ids rather than one made literal.
    """
    with serve(_green_metacharacter_vault) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_EXPECTED_VAULTS="c.s", PREFLIGHT_CHECKS="vault_load"),
            bash_bin=bash_bin,
        )
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode == 0, f"an advertised id must be credited as written:\n{proc.stdout}"
    assert verdicts.get("vault_load") == "PASS", verdicts
    assert _detail(proc.stdout, "vault_load") == _VAULT_LOAD_CLEAN_DETAIL, proc.stdout


@_NEEDS_RUNTIME
@pytest.mark.parametrize("bash_bin", _BASH_BIN_PARAMS)
def test_vault_load_does_not_expand_an_id_against_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bash_bin: str
) -> None:
    """An expected id is never replaced by a filename.

    The split reads the list through an *unquoted* expansion, which is how the
    comma split is obtained -- and pathname expansion applies to that same
    expansion. An id carrying a glob character is therefore replaced by whatever
    the deploy runner's working directory holds, so the ids the loop looks up
    are not the ids the operator wrote.

    That reverses the gate's direction of error. Here ``ca?`` is no vault, but a
    file named ``cas`` sits beside the run, so an expanding split hands the loop
    the advertised ``cas`` and reports every expected vault present -- a green
    vault_load standing on a filename. The failure needs no adversary: the
    variable is operator-edited, and CI runs the harness from a checkout full of
    short slug-shaped names.

    The file is what makes the scenario load-bearing. Without it ``ca?`` matches
    nothing on disk, stays literal, and fails for a reason that has nothing to
    do with expansion -- so the run must happen in a directory that contains the
    bait, which is what ``monkeypatch.chdir`` supplies.
    """
    (tmp_path / "cas").touch()
    monkeypatch.chdir(tmp_path)
    # Positive control on the bait. A bait that does not actually match reverts
    # this scenario to one an expanding split satisfies -- the pattern finds
    # nothing, stays literal, and the assertion below is met for the wrong
    # reason -- and nothing else here would say so.
    assert glob.glob("ca?") == ["cas"], "the bait is not a live glob match; the scenario is inert"
    with serve(_green) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_EXPECTED_VAULTS="ca?", PREFLIGHT_CHECKS="vault_load"),
            bash_bin=bash_bin,
        )
    verdicts = _verdicts(proc.stdout)
    detail = _detail(proc.stdout, "vault_load")
    assert proc.returncode != 0, (
        f"a filename beside the run must not satisfy an expected id:\n{proc.stdout}"
    )
    assert verdicts.get("vault_load") == "FAIL", verdicts
    # The id as written, not the filename it would have expanded to: a detail
    # naming `cas` would mean the expansion happened and the loop merely failed
    # to find it, which is a different script than the one this pins.
    assert detail.endswith("missing expected id(s): ca?"), detail


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
# E. MCP-surface checks (maintenance-mount auth enforcement + mcp_roundtrip)   #
#                                                                              #
# The bash check owns the anti-coincidental CONTROL logic (discovery-200 edge- #
# live + the unauth-401 gate); the round-trip PROTOCOL is stubbed here via the #
# PREFLIGHT_MCP_PROBE_CMD seam and proven for real in test_mcp_preflight_probe. #
# Both maintenance mount paths run the same check body: /mcp_maint (canonical) #
# and /mcp_admin (the pre-rename alias path).                                  #
# --------------------------------------------------------------------------- #
def _discovery_broken(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
    """``_green`` but the OAuth discovery doc 404s -- the dead-edge control trap."""
    if path.split("?", 1)[0] == "/.well-known/oauth-protected-resource":
        return 404, '{"error":"not_found"}', {}
    return _green(method, path, body)


_MAINTENANCE_CHECKS = [("mcp_maint", "/mcp_maint"), ("mcp_admin", "/mcp_admin")]


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
# H. Read-only Core API sweep (deployed REST coverage beyond /sage_vaults)     #
#                                                                             #
# The REST adapter is barely exercised against a live tenant: the web client   #
# covers it only when a human uses it, and the MCP tools call the service      #
# layer in process rather than over HTTP. These checks measure that surface    #
# instead of assuming it. Every probe is a read or a pure computation, and     #
# that property is itself asserted below -- at the wire and in the script text #
# -- rather than left to a source comment.                                     #
# --------------------------------------------------------------------------- #
_SWEEP_CHECKS = "core_api_vault_reads,core_api_document_reads,core_api_parse_filename"

#: Every (method, path-shape) the sweep is permitted to issue. Anything outside
#: this set mutates vault state and must never appear on the wire.
_READ_ONLY_ALLOWLIST: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("GET", "/sage_vaults"),
        ("GET", "/stats"),
        ("GET", "/config"),
        ("GET", "/pending-metadata"),
        ("GET", "/staging-edges"),
        ("GET", "/document"),
        ("GET", "/headings"),
        ("POST", "/discover"),
        ("POST", "/traverse"),
        ("POST", "/parse-filename"),
    }
)

#: Route fragments that mutate. None may appear inside a sweep check's body.
_MUTATING_FRAGMENTS: Final[tuple[str, ...]] = (
    "/edges",
    "/lifecycles",
    "/metadata",
    ":batch",
    "/confirm",
    "/dismiss",
    "/reabstract",
    "/open",
    "/export",
    "/hash-check",
    "/eval-retrieval",
    "/users",
    "/editors",
    "/refresh-views",
    "/admin/",
)


def _classify(method: str, path: str) -> tuple[str, str]:
    """Reduce a recorded request to the (method, path-shape) the allowlist keys on."""
    p = path.split("?", 1)[0]
    if p == "/sage_vaults":
        return method, "/sage_vaults"
    for leaf in ("/stats", "/config", "/pending-metadata", "/staging-edges", "/headings"):
        if p.endswith(leaf):
            return method, leaf
    for leaf in ("/discover", "/traverse", "/parse-filename"):
        if p.endswith(leaf):
            return method, leaf
    if "/documents/" in p:
        return method, "/document"
    return method, p


def _join_continuations(text: str) -> str:
    """Fold ``\\``-continued shell lines into one physical line, so a line-wise
    scan reads the statement the author wrote rather than its formatting."""
    return re.sub(r"\\\n\s*", " ", text)


#: A command substitution whose pipeline reduces its input to a bounded token --
#: an id, a status, a single field. Interpolating a response body *through* one
#: of these is extraction, not disclosure, so the leak scan below strips such
#: spans before looking for the body.
_BOUNDED_EXTRACTORS: Final[tuple[str, ...]] = ("grep -o", "sed", "cut", "awk", "tr ", "head -c")

#: The response-body globals a check must never publish. ``HTTP_BODY`` is the raw
#: payload; ``body`` is the conventional local a check copies it into.
_BODY_TOKENS: Final[tuple[str, ...]] = ("$HTTP_BODY", "${HTTP_BODY}", "$body", "${body}")


def _strip_comment_lines(text: str) -> str:
    """Drop whole-line ``#`` comments so prose naming a function or a variable
    cannot be mistaken for a call or an assignment."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _all_function_bodies(text: str) -> dict[str, str]:
    """Every shell function in ``text``, keyed by name.

    Spans run from the function's ``name() {`` line to the closing ``}`` at
    column 0 -- the script's own formatting, so no parser is needed.
    """
    stripped = _strip_comment_lines(text)
    return {
        m.group(1): m.group(2)
        for m in re.finditer(r"^([a-z_0-9]+)\(\) \{.*?$(.*?)^\}$", stripped, re.S | re.M)
    }


def _reachable_from(roots: Iterable[str], bodies: dict[str, str]) -> dict[str, str]:
    """The call-graph closure of ``roots`` over ``bodies``.

    Keying the sweep's structural gates on reachability rather than on a
    hand-maintained name list is the point: the helper that actually composes a
    failure string is one call away from the check that reports it, and a list
    written when the check was authored does not learn about a helper added
    later. Word-boundary matching is exact here because ``_`` is a word
    character -- ``http_get`` does not match inside ``http_get_browser``.
    """
    seen: dict[str, str] = {}
    queue = [name for name in roots if name in bodies]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen[name] = bodies[name]
        for candidate in bodies:
            if candidate not in seen and re.search(rf"\b{re.escape(candidate)}\b", bodies[name]):
                queue.append(candidate)
    return seen


def _sweep_roots(text: str) -> list[str]:
    """The check functions the registry wires to the sweep's check ids -- read
    out of the ``register`` lines rather than restated here, so a renamed check
    cannot silently drop out of the scan."""
    return re.findall(r"^register\s+core_api_\w+\s+(\w+)", _strip_comment_lines(text), re.M)


def _sweep_function_bodies() -> dict[str, str]:
    """Every function the sweep checks reach, keyed by name."""
    text = _script_text()
    roots = _sweep_roots(text)
    assert roots, "the registry wires no core_api_* check to a function"
    reachable = _reachable_from(roots, _all_function_bodies(text))
    assert set(roots) <= set(reachable), "a registered sweep check is not defined in the harness"
    return reachable


def _reported_variables(bodies: dict[str, str]) -> set[str]:
    """``DETAIL_MSG`` plus every variable interpolated into a ``DETAIL_MSG``.

    Derived rather than listed: a check does not build its matrix line in one
    statement, it accumulates text in a helper variable and interpolates that.
    Whatever flows into ``DETAIL_MSG`` is as published as ``DETAIL_MSG`` itself,
    so the leak scan has to cover it -- and has to keep covering it when a
    later accumulator is added under a name nobody wrote down here.
    """
    reported = {"DETAIL_MSG"}
    for body in bodies.values():
        for line in _join_continuations(body).splitlines():
            m = re.search(r"DETAIL_MSG=\"(.*)\"\s*$", line)
            if not m:
                continue
            reported.update(re.findall(r"\$\{?([A-Za-z_][A-Za-z_0-9]*)\}?", m.group(1)))
    return reported


def _body_leak_findings(bodies: dict[str, str]) -> list[str]:
    """Assignments that would publish a response body on the matrix line.

    The sweep reads ``GET .../config``, which returns the vault's full
    configuration, and preflight output lands in a deploy log. A finding is any
    assignment to a reported variable that interpolates the body outside a
    bounded extraction -- so pulling an id out of a payload is allowed and
    pasting the payload into a message is not.

    ``DETAIL_MSG`` is exempt from that allowance: it *is* the matrix line, and no
    extraction is narrow enough to justify composing it out of a response body.
    Scoping the allowance to the derived variables that actually need it keeps
    this scan from relaxing the rule it inherited while widening the surface.
    """
    reported = _reported_variables(bodies)
    findings: list[str] = []
    for name, body in bodies.items():
        for line in _join_continuations(body).splitlines():
            m = re.match(r"\s*(?:local\s+)?([A-Za-z_][A-Za-z_0-9]*)=", line)
            if not m or m.group(1) not in reported:
                continue
            if m.group(1) == "DETAIL_MSG":
                residue = line
            else:
                residue = re.sub(
                    r"\$\((?:[^()]|\([^()]*\))*\)",
                    lambda s: (
                        "" if any(e in s.group(0) for e in _BOUNDED_EXTRACTORS) else s.group(0)
                    ),
                    line,
                )
            for token in _BODY_TOKENS:
                if token in residue:
                    findings.append(f"{name}: {line.strip()}")
                    break
    return findings


# --- Side-effect freedom (the acceptance criterion, made testable) ---------- #
def _post_targets(body: str) -> list[str]:
    """Every URL an ``http_post`` in ``body`` actually targets.

    Shell variables assigned inside the function are substituted first, so the
    assertion is about the endpoint the call reaches rather than about whether
    the author happened to inline the URL at the call site. A gate that only
    matched the literal call line would pass or fail on formatting.
    """
    assignments = dict(re.findall(r"(?:local\s+)?(\w+)=\"([^\"]*)\"", body))
    # One expansion pass resolves the nesting these helpers actually use
    # (a url built from a base, which is built from an env var).
    for _ in range(3):
        for key, value in list(assignments.items()):
            assignments[key] = re.sub(
                r"\$\{?(\w+)\}?", lambda m: assignments.get(m.group(1), m.group(0)), value
            )
    targets: list[str] = []
    # A call may wrap onto a continuation line; join them before matching.
    for line in _join_continuations(body).splitlines():
        m = re.search(r'http_post\s+"([^"]*)"', line)
        if m:
            targets.append(
                re.sub(
                    r"\$\{?(\w+)\}?", lambda g: assignments.get(g.group(1), g.group(0)), m.group(1)
                )
            )
    return targets


def test_core_api_sweep_text_carries_no_mutating_route() -> None:
    """Structural leg: no sweep check names a mutating route, and its only POSTs
    go to the three side-effect-free endpoints. Always on -- needs no network.
    """
    for name, body in _sweep_function_bodies().items():
        for fragment in _MUTATING_FRAGMENTS:
            assert fragment not in body, f"{name} names the mutating route {fragment}"
        for target in _post_targets(body):
            assert any(
                endpoint in target for endpoint in ("/discover", "/traverse", "/parse-filename")
            ), f"{name} POSTs to something other than the read-only endpoints: {target}"
        assert "http_put" not in body, f"{name} issues a PUT"
        assert "-X DELETE" not in body, f"{name} issues a DELETE"


# --- Shape markers bound to the authoritative response schema -------------- #
#: Each ``sweep_get`` label, and the Core API operation whose 200 response its
#: shape marker is asserting. Mirroring a marker between the shipped script and
#: a stub proves only that the two copies agree; resolving it against the
#: committed document proves it names something the deployment actually returns.
_SWEEP_MARKER_TABLE: Final[dict[str, tuple[str, str]]] = {
    "/stats": ("get", "/sage_vaults/{vault_id}/stats"),
    "/config": ("get", "/sage_vaults/{vault_id}/config"),
    "/pending-metadata": ("get", "/sage_vaults/{vault_id}/pending-metadata"),
    "/staging-edges": ("get", "/sage_vaults/{vault_id}/staging-edges"),
    "/documents/{id}": ("get", "/sage_vaults/{vault_id}/documents/{document_id}"),
    "/documents/{id}/headings": (
        "get",
        "/sage_vaults/{vault_id}/documents/{document_id}/headings",
    ),
}

#: GET /config returns an untyped dict, so the OpenAPI document defers its shape
#: to the vault-config JSON Schema. That schema is this label's authority.
_SCHEMA_BACKED_LABELS: Final[frozenset[str]] = frozenset({"/config"})

#: Each stub response body, and the operation whose 200 schema it stands in for.
#: Binding the stub side too is what stops the pair drifting together: a
#: response model that gains a required field reds the stub that omits it.
_STUB_BODY_AUTHORITY: Final[dict[str, tuple[str, str]]] = {
    "_STATS_BODY": ("get", "/sage_vaults/{vault_id}/stats"),
    "_CONFIG_BODY": ("get", "/sage_vaults/{vault_id}/config"),
    "_DOCUMENT_BODY": ("get", "/sage_vaults/{vault_id}/documents/{document_id}"),
    "_HEADINGS_BODY": ("get", "/sage_vaults/{vault_id}/documents/{document_id}/headings"),
    "_TRAVERSE_BODY": ("post", "/sage_vaults/{vault_id}/traverse"),
    "_PARSE_BODY": ("post", "/sage_vaults/{vault_id}/parse-filename"),
    "_DISCOVER_OK": ("post", "/sage_vaults/{vault_id}/discover"),
}

#: The marker the sweep uses where a response is a bare JSON array rather than
#: an object with a named field.
_ARRAY_MARKER: Final[str] = r"^[[:space:]]*\["

_SPEC_CACHE: dict[str, dict] = {}


def _openapi_document() -> dict:
    if "spec" not in _SPEC_CACHE:
        _SPEC_CACHE["spec"] = yaml.safe_load(_OPENAPI_SPEC.read_text(encoding="utf-8"))
    return _SPEC_CACHE["spec"]


def _vault_config_schema() -> dict:
    if "vault_config" not in _SPEC_CACHE:
        _SPEC_CACHE["vault_config"] = json.loads(_VAULT_CONFIG_SCHEMA.read_text(encoding="utf-8"))
    return _SPEC_CACHE["vault_config"]


def _deref(schema: dict) -> dict:
    """Follow a ``$ref`` into ``components/schemas``."""
    ref = schema.get("$ref")
    if not ref:
        return schema
    name = ref.rsplit("/", 1)[-1]
    resolved = _openapi_document()["components"]["schemas"].get(name)
    assert resolved is not None, f"the document has no component schema {name}"
    return resolved


def _response_schema(method: str, path: str) -> dict:
    """The 200 response schema of one operation, with a ``$ref`` followed once."""
    operation = _openapi_document()["paths"].get(path, {}).get(method)
    assert operation is not None, f"the document has no {method.upper()} {path}"
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    return _deref(schema)


def _required_and_properties(schema: dict) -> tuple[set[str], set[str]]:
    """The required and declared property names of a schema, flattening one
    level of ``allOf`` -- the composition FastAPI emits for a model that extends
    another, and where several of the sweep's fields actually live."""
    required: set[str] = set(schema.get("required", []))
    properties: set[str] = set(schema.get("properties", {}))
    for member in schema.get("allOf", []):
        member_required, member_properties = _required_and_properties(_deref(member))
        required |= member_required
        properties |= member_properties
    return required, properties


def _authority_for(label: str) -> tuple[set[str], set[str], dict]:
    """``(required, properties, schema)`` for a sweep label."""
    if label in _SCHEMA_BACKED_LABELS:
        schema = _vault_config_schema()
    else:
        schema = _response_schema(*_SWEEP_MARKER_TABLE[label])
    required, properties = _required_and_properties(schema)
    return required, properties, schema


def _sweep_get_call_sites(text: str) -> list[tuple[str, str]]:
    """``(label, marker)`` for every ``sweep_get`` call in ``text``.

    The marker is returned with its shell quoting stripped, so the assertion is
    about the pattern the check greps for rather than about how it was quoted.
    """
    pattern = re.compile(r"""sweep_get\s+"[^"]*"\s+"([^"]*)"\s+('[^']*'|"(?:\\.|[^"\\])*")""")
    sites: list[tuple[str, str]] = []
    for label, raw in pattern.findall(_join_continuations(_strip_comment_lines(text))):
        marker = raw[1:-1]
        if raw.startswith('"'):
            marker = marker.replace('\\"', '"')
        sites.append((label, marker))
    return sites


def test_sweep_marker_table_covers_every_sweep_get() -> None:
    """Every shipped call site is bound, and no table entry outlives its call
    site. Also pins the count: an extractor that found nothing would make every
    binding assertion below pass vacuously.
    """
    sites = _sweep_get_call_sites(_script_text())
    assert len(sites) == 6, f"expected 6 sweep_get call sites, found {len(sites)}: {sites}"
    assert {label for label, _ in sites} == set(_SWEEP_MARKER_TABLE), (
        f"call sites {sorted({label for label, _ in sites})} vs table {sorted(_SWEEP_MARKER_TABLE)}"
    )


def test_sweep_marker_paths_exist_in_the_openapi_document() -> None:
    """Every bound operation is one the committed document actually declares --
    so a renamed or retired route reds the gate here rather than on a tenant."""
    for label, (method, path) in _SWEEP_MARKER_TABLE.items():
        schema = _response_schema(method, path)
        assert schema, f"{label}: {method.upper()} {path} declares no 200 schema"


@pytest.mark.parametrize("label,marker", _sweep_get_call_sites(_SCRIPT.read_text(encoding="utf-8")))
def test_sweep_marker_resolves_against_its_response_schema(label: str, marker: str) -> None:
    """Each shape marker names something the operation's 200 response is
    guaranteed to carry.

    Three marker forms, one rule each: a JSON field marker must be a *required*
    property (an optional one would let a healthy response miss it); the array
    marker must front an array-typed response; and the echoed-document-id marker
    must have a required ``id`` to echo.
    """
    required, _, schema = _authority_for(label)
    if marker == _ARRAY_MARKER:
        assert schema.get("type") == "array", f"{label}: array marker on a non-array response"
        return
    if "$DOC_FIRST_ID" in marker:
        assert "id" in required, f"{label}: the echoed id is not a required response property"
        return
    field = marker.strip('"')
    assert field in required, (
        f"{label}: marker '{field}' is not a required property of the 200 response "
        f"(required: {sorted(required)})"
    )


def test_config_marker_is_a_required_vault_config_section() -> None:
    """The one label whose response the document types only as an object: its
    marker is resolved against the vault-config schema instead."""
    required, _, _ = _authority_for("/config")
    assert "edge_inference" in required


def test_marker_binding_detects_a_phantom_field() -> None:
    """The teeth test: a marker naming a field no response model declares is
    rejected. Without it, an authority that resolved to an empty required set
    would pass every marker above.
    """
    with pytest.raises(AssertionError):
        test_sweep_marker_resolves_against_its_response_schema("/stats", '"totally_not_a_field"')


def test_marker_binding_rejects_a_declared_but_optional_field() -> None:
    """The teeth test the phantom-field case cannot supply.

    A phantom name is absent from both the required list and the property list,
    so a resolver that returned *properties* instead of *required* would reject
    it too -- and would then accept a marker naming a merely-optional field. A
    healthy deployment may omit such a field, so the sweep would red on a
    correct tenant, which is the exact defect class this binding exists to
    prevent. `last_optimize` is declared on the stats response and not required.
    """
    _, properties, _ = _authority_for("/stats")
    assert "last_optimize" in properties, (
        "the probe field is no longer declared; pick another optional property"
    )
    with pytest.raises(AssertionError):
        test_sweep_marker_resolves_against_its_response_schema("/stats", '"last_optimize"')


def test_body_leak_detector_does_not_exempt_a_detail_msg_extraction() -> None:
    """The matrix line takes no extraction allowance.

    Deriving an accumulator's value from a payload through a bounded pipeline is
    legitimate; composing the reported line itself that way is not, and the scan
    this one replaced had no such allowance. Without this case, widening the
    surface would have quietly relaxed the rule on its most exposed variable.
    """
    leaking_detail = """
check_probe() {
  DETAIL_MSG="$(printf '%s' "$HTTP_BODY" | grep -oE '.*')"
  return 1
}
"""
    assert _body_leak_findings(_all_function_bodies(leaking_detail))


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ('{"headings":[],"title":"Probe"}', "document_id"),
        ('{"document_id":"x","title":"Probe","headings":[],"invented":1}', "invented"),
    ],
    ids=["omits-required", "invents-property"],
)
def test_stub_conformance_detects_a_drifted_stub(mutation: str, expected: str) -> None:
    """The teeth test for both halves of the stub binding: a stub that drops a
    required property and one that invents a property each have to be caught, or
    the conformance check below passes on stubs it never really constrained.
    """
    required, properties = _required_and_properties(
        _response_schema(*_STUB_BODY_AUTHORITY["_HEADINGS_BODY"])
    )
    keys = set(json.loads(mutation))
    assert not (required <= keys and keys <= properties), (
        f"a stub drifted on '{expected}' would pass the conformance check"
    )


@pytest.mark.parametrize("stub_name", sorted(_STUB_BODY_AUTHORITY))
def test_stub_bodies_conform_to_their_response_schema(stub_name: str) -> None:
    """The other half of the binding: each stub body carries every required
    property of the response it stands in for, and invents none.

    A stub is what the behavioral tests assert against, so a stub that has
    drifted from the response model makes those tests prove something about a
    shape no deployment serves.
    """
    method, path = _STUB_BODY_AUTHORITY[stub_name]
    required, properties = _required_and_properties(_response_schema(method, path))
    if stub_name == "_CONFIG_BODY":
        required, properties = _required_and_properties(_vault_config_schema())
    payload = json.loads(globals()[stub_name])
    keys = set(payload)
    assert required <= keys, f"{stub_name} omits required {sorted(required - keys)}"
    assert keys <= properties, f"{stub_name} invents {sorted(keys - properties)}"


def test_core_api_sweep_never_reports_a_response_body() -> None:
    """Nothing the sweep reaches may put a response body on the matrix line.

    The sweep reads GET .../config, which returns the vault's full configuration.
    Preflight output lands in a deploy log, so a detail message that interpolated
    a body would publish that configuration. Every check must report the endpoint
    and the observed status instead -- the same discipline the token diagnostic
    keeps when it prints decoded claims but never the token.

    The scan runs over the call-graph closure, not over the checks alone: the
    strings a check reports are composed one call away, in a shared helper, and
    a scan that stopped at the check would read the interpolation and never the
    text being interpolated.
    """
    findings = _body_leak_findings(_sweep_function_bodies())
    assert not findings, f"a response body reaches the matrix line: {findings}"


def test_sweep_reachable_set_includes_shared_helpers() -> None:
    """The closure the structural gates scan actually contains the helpers the
    sweep composes its output in -- the coverage a name list silently lost."""
    reachable = _sweep_function_bodies()
    for helper in ("sweep_get", "resolve_probe_vault", "resolve_probe_document"):
        assert helper in reachable, f"{helper} is outside the scanned closure"
    for transport in ("http_get", "http_post"):
        assert transport in reachable, f"{transport} is outside the scanned closure"


def test_reported_variable_closure_is_derived_not_listed() -> None:
    """``SWEEP_FAILURES`` is in the reported set because the script interpolates
    it into a DETAIL_MSG, not because this file names it. A later accumulator
    joins the set the same way."""
    reported = _reported_variables(_sweep_function_bodies())
    assert "DETAIL_MSG" in reported
    assert "SWEEP_FAILURES" in reported, (
        "the accumulator the sweep reports through is outside the leak scan"
    )


_LEAKING_HELPER = """
check_core_api_probe() {
  ACCUMULATOR=""
  probe_one "$base/config"
  DETAIL_MSG="reads failed:$ACCUMULATOR"
  return 1
}

probe_one() {
  ACCUMULATOR="$ACCUMULATOR $1 $HTTP_BODY;"
  return 0
}
"""

_EXTRACTING_HELPER = """
check_core_api_probe() {
  ACCUMULATOR=""
  probe_one "$base/config"
  DETAIL_MSG="read '$ACCUMULATOR'"
  return 1
}

probe_one() {
  ACCUMULATOR="$(printf '%s' "$HTTP_BODY" | grep -oE '"[0-9a-f]{8}_[a-z0-9_]+"' | head -1)"
  return 0
}
"""


def test_body_leak_detector_fires() -> None:
    """The teeth test: a body pasted into an accumulator inside a *helper* --
    never into a DETAIL_MSG directly -- is caught. This is exactly the edit the
    name-list scan would have passed.
    """
    findings = _body_leak_findings(_all_function_bodies(_LEAKING_HELPER))
    assert findings, "the detector missed a response body reaching the matrix line"
    assert any("probe_one" in f for f in findings), findings


def test_body_leak_detector_permits_bounded_extraction() -> None:
    """The false-positive guard: pulling a document id out of a payload through
    a bounded pipeline is extraction, not disclosure. A detector that flagged it
    would force the sweep to stop resolving a document at all.
    """
    assert not _body_leak_findings(_all_function_bodies(_EXTRACTING_HELPER))


@_NEEDS_RUNTIME
def test_core_api_sweep_issues_no_mutating_request() -> None:
    """Behavioral leg: record every request a full sweep puts on the wire and
    assert each one is in the read-only allowlist. This is the assertion the
    structural scan cannot make -- it observes what the script actually did,
    not what its source says.
    """
    seen: list[tuple[str, str]] = []

    def recording(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        seen.append(_classify(method, path))
        return _green(method, path, body)

    with serve(recording) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS=_SWEEP_CHECKS))
    assert proc.returncode == 0, f"the sweep must pass against a healthy tenant:\n{proc.stdout}"
    assert seen, "the sweep issued no requests at all"
    outside = set(seen) - _READ_ONLY_ALLOWLIST
    assert not outside, f"the sweep issued non-read requests: {sorted(outside)}"


# --- core_api_vault_reads --------------------------------------------------- #
@_NEEDS_RUNTIME
def test_core_api_vault_reads_passes() -> None:
    """All four vault-scoped reads 200 with their shape fields -> PASS."""
    with serve(_green) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_vault_reads"))
    assert _verdicts(proc.stdout).get("core_api_vault_reads") == "PASS", proc.stdout


@_NEEDS_RUNTIME
@pytest.mark.parametrize("endpoint", ["/stats", "/config", "/pending-metadata", "/staging-edges"])
def test_core_api_vault_reads_fails_on_endpoint_404(endpoint: str) -> None:
    """One endpoint 404s while the rest stay healthy -> FAIL, and the detail
    names that endpoint AND the observed code. A message that only said "a read
    failed" would leave an operator to bisect the sweep by hand.
    """

    def broken(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0].endswith(endpoint):
            return 404, '{"error":"not_found"}', {}
        return _green(method, path, body)

    with serve(broken) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_vault_reads"))
    detail = _detail(proc.stdout, "core_api_vault_reads")
    assert _verdicts(proc.stdout).get("core_api_vault_reads") == "FAIL", proc.stdout
    assert endpoint in detail, f"detail does not name the endpoint: {detail}"
    assert "404" in detail, f"detail does not name the observed code: {detail}"


@_NEEDS_RUNTIME
def test_core_api_vault_reads_fails_when_absent_vault_returns_200() -> None:
    """THE anti-coincidental test for the sweep: every real read is healthy, but
    the edge also 200s a vault that does not exist. Four green reads must not be
    credited when the endpoint cannot tell a real vault from an absent one.
    """

    def absent_vault_200(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0].startswith(f"/sage_vaults/{_ABSENT_VAULT}"):
            return 200, _STATS_BODY, {}
        return _green(method, path, body)

    with serve(absent_vault_200) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_vault_reads"))
    assert _verdicts(proc.stdout).get("core_api_vault_reads") == "FAIL", proc.stdout
    assert "control" in _detail(proc.stdout, "core_api_vault_reads").lower()


@_NEEDS_RUNTIME
def test_core_api_vault_reads_fails_on_wrong_shape() -> None:
    """/stats answers 200 with a body that is not a VaultStatsResponse -> FAIL.
    Proves the check reads the payload, not merely the status line.
    """

    def canned(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0].endswith("/stats"):
            return 200, '{"ok":true}', {}
        return _green(method, path, body)

    with serve(canned) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_vault_reads"))
    assert _verdicts(proc.stdout).get("core_api_vault_reads") == "FAIL", proc.stdout


@_NEEDS_RUNTIME
def test_core_api_vault_reads_skips_when_no_vault_available() -> None:
    """An empty vault registry is vault_load's failure to report, not this
    check's -- it SKIPs so one root cause does not over-paint the matrix.
    """

    def empty_vaults(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0] == "/sage_vaults":
            return 200, "[]", {}
        return _green(method, path, body)

    with serve(empty_vaults) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_vault_reads"))
    assert _verdicts(proc.stdout).get("core_api_vault_reads") == "SKIP", proc.stdout


# --- core_api_document_reads ------------------------------------------------ #
@_NEEDS_RUNTIME
def test_core_api_document_reads_passes() -> None:
    """A document resolves from the catalog probe and all three document-scoped
    reads answer 200 with their shape fields -> PASS.
    """
    with serve(_green) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_document_reads"))
    assert _verdicts(proc.stdout).get("core_api_document_reads") == "PASS", proc.stdout


@_NEEDS_RUNTIME
def test_core_api_document_reads_skips_on_empty_vault() -> None:
    """A deployment whose vault holds no documents must not fail the sweep: the
    catalog probe answers 200 with nothing to read, so the check SKIPs and the
    run still exits 0.
    """

    def no_documents(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0].endswith("/discover") and b"deterministic" not in body:
            return 200, '{"mode":"catalog","results":[],"total_available":0}', {}
        return _green(method, path, body)

    with serve(no_documents) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_document_reads"))
    assert _verdicts(proc.stdout).get("core_api_document_reads") == "SKIP", proc.stdout
    assert proc.returncode == 0, "an empty vault must not fail the preflight run"


@_NEEDS_RUNTIME
def test_core_api_document_reads_fails_when_discover_broken() -> None:
    """The control that keeps the empty-vault SKIP honest: a NON-200 catalog
    probe is a broken endpoint, not an empty vault, and must FAIL. Without this
    split, a 500 would hide behind the same SKIP as a legitimately empty vault.
    """

    def discover_500(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0].endswith("/discover"):
            return 500, '{"error":"storage_unavailable"}', {}
        return _green(method, path, body)

    with serve(discover_500) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_document_reads"))
    detail = _detail(proc.stdout, "core_api_document_reads")
    assert _verdicts(proc.stdout).get("core_api_document_reads") == "FAIL", proc.stdout
    assert "500" in detail, detail


@_NEEDS_RUNTIME
def test_core_api_document_reads_fails_when_absent_document_returns_200() -> None:
    """Every real read is healthy, but the edge also 200s a document id that does
    not exist -- the reads prove nothing and must not be credited.
    """

    def absent_doc_200(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        p = path.split("?", 1)[0]
        if _ABSENT_DOC in p or (p.endswith("/traverse") and _ABSENT_DOC.encode() in body):
            return 200, _DOCUMENT_BODY, {}
        return _green(method, path, body)

    with serve(absent_doc_200) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_document_reads"))
    assert _verdicts(proc.stdout).get("core_api_document_reads") == "FAIL", proc.stdout
    assert "control" in _detail(proc.stdout, "core_api_document_reads").lower()


@_NEEDS_RUNTIME
def test_core_api_document_reads_fails_on_traverse_error() -> None:
    """The graph leg is exercised independently of the content legs: traverse 500
    while the document reads stay green -> FAIL naming traverse and the code.
    """

    def traverse_500(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0].endswith("/traverse"):
            return 500, '{"error":"storage_unavailable"}', {}
        return _green(method, path, body)

    with serve(traverse_500) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_document_reads"))
    detail = _detail(proc.stdout, "core_api_document_reads")
    assert _verdicts(proc.stdout).get("core_api_document_reads") == "FAIL", proc.stdout
    assert "traverse" in detail, detail
    assert "500" in detail, detail


@_NEEDS_RUNTIME
def test_core_api_document_reads_tolerates_no_projection() -> None:
    """A document that exists but has no stored projection is a legitimate state
    on a healthy tenant -- ingestion mid-pipeline, or a document awaiting
    reabstraction -- and the headings route reports it as 404 `no_projection`.

    The probe reads whichever document a limit:1 catalog page returns, so which
    document it lands on is not the harness's to choose. Failing there would red
    a healthy tenant on a property of its first catalog row.
    """

    def no_projection(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0].endswith("/headings"):
            return 404, '{"code":"no_projection","message":"No projection stored"}', {}
        return _green(method, path, body)

    with serve(no_projection) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_document_reads"))
    detail = _detail(proc.stdout, "core_api_document_reads")
    assert _verdicts(proc.stdout).get("core_api_document_reads") == "PASS", proc.stdout
    assert "no_projection" in detail, f"the tolerated outcome is not reported: {detail}"
    assert proc.returncode == 0


@_NEEDS_RUNTIME
def test_core_api_document_reads_fails_when_headings_reports_document_not_found() -> None:
    """The same status, a different meaning: the document read just succeeded on
    this id, so `document_not_found` from the headings route is a real fault.
    Tolerance is keyed on the error code, never on the status alone -- otherwise
    it would swallow every 404 the route can produce.
    """

    def wrong_code(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0].endswith("/headings"):
            return 404, '{"code":"document_not_found","message":"No such document"}', {}
        return _green(method, path, body)

    with serve(wrong_code) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_document_reads"))
    detail = _detail(proc.stdout, "core_api_document_reads")
    assert _verdicts(proc.stdout).get("core_api_document_reads") == "FAIL", proc.stdout
    assert "/headings" in detail, detail


@_NEEDS_RUNTIME
def test_core_api_document_reads_fails_when_headings_500s() -> None:
    """The tolerance must not have stopped the check reading the headings leg at
    all: an endpoint fault still FAILs, naming the route and the code.
    """

    def headings_500(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0].endswith("/headings"):
            return 500, '{"code":"internal"}', {}
        return _green(method, path, body)

    with serve(headings_500) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_document_reads"))
    detail = _detail(proc.stdout, "core_api_document_reads")
    assert _verdicts(proc.stdout).get("core_api_document_reads") == "FAIL", proc.stdout
    assert "/headings" in detail, detail
    assert "500" in detail, detail


@_NEEDS_RUNTIME
def test_no_projection_tolerance_is_scoped_to_the_headings_leg() -> None:
    """Only the headings route can legitimately answer `no_projection`. The same
    code from the document read means the store cannot return a record it just
    listed in the catalog -- a fault, and it must stay red.
    """

    def document_no_projection(
        method: str, path: str, body: bytes
    ) -> tuple[int, str, dict[str, str]]:
        p = path.split("?", 1)[0]
        if "/documents/" in p and not p.endswith("/headings"):
            return 404, '{"code":"no_projection","message":"No projection stored"}', {}
        return _green(method, path, body)

    with serve(document_no_projection) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_document_reads"))
    assert _verdicts(proc.stdout).get("core_api_document_reads") == "FAIL", proc.stdout


# --- core_api_parse_filename ------------------------------------------------ #
@_NEEDS_RUNTIME
def test_core_api_parse_filename_passes() -> None:
    """The pure-computation endpoint answers 200 with a parse result -> PASS.

    The assertion is on object shape only, never on a non-null title: the service
    returns an all-null response when the vault configures no
    filename_extraction pattern, which is a legitimate tenant configuration.
    """
    with serve(_green) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_parse_filename"))
    assert _verdicts(proc.stdout).get("core_api_parse_filename") == "PASS", proc.stdout


@_NEEDS_RUNTIME
def test_core_api_parse_filename_fails_when_malformed_body_accepted() -> None:
    """The request-validation control: a body whose source_type is not a
    SourceType member must be rejected. An edge that 200s it is not processing
    request bodies, so the healthy-looking parse above proves nothing.
    """

    def accepts_anything(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0].endswith("/parse-filename"):
            return 200, _PARSE_BODY, {}
        return _green(method, path, body)

    with serve(accepts_anything) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_parse_filename"))
    assert _verdicts(proc.stdout).get("core_api_parse_filename") == "FAIL", proc.stdout
    assert "control" in _detail(proc.stdout, "core_api_parse_filename").lower()


@_NEEDS_RUNTIME
def test_core_api_parse_filename_fails_on_non_200() -> None:
    """A 500 from the parser endpoint -> FAIL naming the observed code."""

    def parse_500(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0].endswith("/parse-filename"):
            return 500, '{"error":"internal"}', {}
        return _green(method, path, body)

    with serve(parse_500) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_parse_filename"))
    detail = _detail(proc.stdout, "core_api_parse_filename")
    assert _verdicts(proc.stdout).get("core_api_parse_filename") == "FAIL", proc.stdout
    assert "500" in detail, detail
