"""Structural and inventory gate for ``deploy/cloud-preflight.sh``.

Reads the script text and enumerates its check registry via ``--dry-run``, and
proves the text detectors can fail. The behavioral scenarios live in the sibling
``test_cloud_preflight_*.py`` modules; ``_preflight_harness.py`` describes the
gate as a whole and carries the machinery they share.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Final

from tests.deploy._preflight_harness import (
    _BASH,
    _NEEDS_BASH,
    _REPO_ROOT,
    _SCRIPT,
    _run,
    _script_text,
    _verdicts,
)

#: The sibling helper the resource-registration check's token-probe seam points
#: at in CI: asks the authorization server for a token scoped to one advertised
#: resource, proving (by refusal) whether its identifier URI is registered.
_PROBE_SCRIPT: Final[Path] = _REPO_ROOT / "deploy" / "resource-token-probe.sh"

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
        "mcp_admin_retired",
        "mcp_roundtrip",
        "edge_authn_backend",
        "liveness",
        "ocr_capability",
        "transfer_upload_gate",
        "transfer_download_gate",
        "vault_load",
        "retrieval_pg",
        "postgres_major",
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
    except subprocess.CalledProcessError, FileNotFoundError:
        return None
    match = re.search(r"[:/]([^/]+)/[^/]+?(?:\.git)?$", url)
    return match.group(1) if match else None


def _dry_run_rows(stdout: str) -> dict[str, tuple[str, str]]:
    """Parse ``CHECK <id> | prediction: <p> | control: <c>`` lines."""
    out: dict[str, tuple[str, str]] = {}
    for line in stdout.splitlines():
        m = re.match(r"^CHECK\s+(\w+)\s*\|\s*prediction:\s*(.+?)\s*\|\s*control:\s*(.+?)\s*$", line)
        if m:
            out[m.group(1)] = (m.group(2), m.group(3))
    return out


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
