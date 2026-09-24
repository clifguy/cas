"""Shared harness for the ``deploy/cloud-preflight.sh`` gate modules.

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

The gate is split across the ``test_cloud_preflight*.py`` modules by theme, so
that no one module holds a test worker for most of a parallel run. This module
carries what more than one of them uses: the script runner and its output
parsers, the stub HTTP server, the stub-command writers, and the all-green
responder with the bodies it serves.
"""

from __future__ import annotations

import http.server
import inspect
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import pytest

from tests.deploy._stub_server import serve_threaded

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_SCRIPT: Final[Path] = _REPO_ROOT / "deploy" / "cloud-preflight.sh"

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
        except OSError, subprocess.SubprocessError:
            continue
        if re.fullmatch(r"\d+\.\d+", version):
            found.append((version, str(path)))
    return tuple(found)


#: The interpreters the version-sensitive scenarios run under. Kept as
#: ``(label, path)`` pairs so a parametrized case is named by the bash version it
#: proves rather than by a filesystem path that says nothing about the floor.
_BASH_BINS: Final[tuple[tuple[str, str], ...]] = _bash_inventory()


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _script_text() -> str:
    return _SCRIPT.read_text(encoding="utf-8")


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
                # assumed. A sixth Authorization parameter observes both auth legs.
                # Existing responders are called unchanged.
                arity = len(inspect.signature(responder).parameters)
                if arity >= 6:
                    status, text, headers = responder(
                        method,
                        self.path,
                        body,
                        self.headers.get("Accept", ""),
                        self.headers.get("Origin", ""),
                        self.headers.get("Authorization", ""),
                    )
                elif arity == 5:
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
# The all-green tenant: the bodies it serves and the responder serving them  #
# --------------------------------------------------------------------------- #
_DISCOVERY_BODY = (
    '{"resource":"https://sage.test.invalid","authorization_servers":'
    '["https://login.microsoftonline.com/t/v2.0"],"scopes_supported":["Sage.Access"]}'
)
_VAULTS_BODY = '{"vaults":[{"id":"cas","name":"CAS"},{"id":"test","name":"Test"}],"count":2}'
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
    '"content_store_small_fragment_count":0,"health":"ok",'
    # Required-and-nullable: the schema lists it, so a conforming body carries
    # the key whatever its value. A vault that has never ingested sends null.
    '"last_ingestion_at":null}'
)
#: GET /config returns VaultConfig.model_dump(), so the schema's required
#: top-level sections are always present; edge_inference is the one asserted.
_CONFIG_BODY = (
    '{"vault":{"id":"cas"},"document_types":[],"lifecycle":{},'
    '"metadata_extraction":{},"edge_inference":{"tier_assignments":[]}}'
)
_PENDING_METADATA_BODY = (
    '{"items":[],"total_available":0,"limit":10,"offset":0,"response_mode":"full"}'
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
        return 200, _PENDING_METADATA_BODY, {}
    if rest == "/staging-edges":
        return 200, '{"items":[],"count":0}', {}
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
    if p in ("/mcp", "/mcp_maint"):
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
