"""Structural gate for the per-tenant parameter-file template.

Locks the shape of ``infra/main.bicepparam.example`` — the documented
per-tenant parameter set an operator copies and fills to bring up a new cloud
tenant. The deployment-profile model the template realizes is recorded in
CAS-ADR-042; the document-store vault-source coordinates it carries are
governed by CAS-ADR-043.

The template documents the *full* parameter surface of ``infra/main.bicep``,
including the values supplied out of band — the subscription and tenant id via
the deploy identity's OIDC federation, and the secrets and TLS certificate via
the Key Vault loader. Every identity-bearing value is a ``REPLACE-WITH-*``
placeholder, so the template carries no tenant coordinate of its own.

These checks read tracked files only — no Azure tooling and no live tenant — so
they run in the ordinary Python test job alongside the infra scaffolding gate.
The authoritative Bicep validation is the infra workflow's ``validate`` job; a
local fast-path compile is provided here, skipped when the CLI is absent.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
INFRA_DIR: Final[Path] = REPO_ROOT / "infra"
MAIN_BICEP: Final[Path] = INFRA_DIR / "main.bicep"
TEMPLATE: Final[Path] = INFRA_DIR / "main.bicepparam.example"

# A subscription / tenant / client / object id is a GUID. None may be baked
# into the template — every identity value is a REPLACE-WITH-* placeholder the
# operator fills at deploy time.
_GUID_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b"
)

# Identity-bearing parameters: their values are tenant coordinates (an audience
# URI, a client/object GUID, an owned domain, a Microsoft Graph site/drive id),
# so they must be REPLACE-WITH-* placeholders in the template, never literals.
_IDENTITY_PARAMS: Final[tuple[str, ...]] = (
    "sageAudience",
    "bffOidcClientId",
    "mcpClientId",
    "baseDomain",
    "postgresAadAdminObjectId",
    "sharepointSiteId",
    "sharepointDriveId",
)


def _git_owner() -> str | None:
    """Derive the repository owner from the origin remote, or ``None``.

    Resolved at runtime so this durable surface carries no personal-identity
    literal of its own.
    """
    try:
        url = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    match = re.search(r"[:/]([^/]+)/[^/]+?(?:\.git)?$", url)
    return match.group(1) if match else None


def _bicep_params() -> dict[str, bool]:
    """Map each ``main.bicep`` parameter name to whether it is required.

    A parameter is required when its declaration line carries no ``=`` default.
    Returns ``{name: is_required}``.
    """
    params: dict[str, bool] = {}
    for line in MAIN_BICEP.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*param\s+(\w+)\b(.*)$", line)
        if not match:
            continue
        params[match.group(1)] = "=" not in match.group(2)
    return params


def _template_text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def test_template_exists_and_wires_to_main() -> None:
    """The template is the per-tenant parameter set, bound to the orchestrator."""
    assert TEMPLATE.is_file(), "infra/main.bicepparam.example missing"
    assert re.search(r"using\s+'\./main\.bicep'", _template_text()), (
        "template must declare `using './main.bicep'`"
    )


def test_template_documents_full_parameter_set() -> None:
    """Every parameter ``main.bicep`` declares is documented in the template, and
    every required parameter (no default) is set — so an operator filling the
    template cannot silently miss a value.
    """
    text = _template_text()
    params = _bicep_params()
    assert params, "no params parsed from main.bicep (parser drift?)"
    for name, required in params.items():
        assert name in text, f"template omits parameter {name!r} declared in main.bicep"
        if required:
            assert re.search(rf"^\s*param\s+{re.escape(name)}\s*=", text, re.M), (
                f"required parameter {name!r} must be set in the template"
            )


def test_template_surfaces_the_two_missing_postgres_admin_params() -> None:
    """The template sets the two Postgres-admin parameters the working
    ``main.bicepparam`` never surfaced — the concrete gap the audit found.
    """
    text = _template_text()
    for name in ("postgresAadAdminObjectId", "postgresAadAdminPrincipalName"):
        assert re.search(rf"^\s*param\s+{re.escape(name)}\s*=", text, re.M), (
            f"template must surface {name!r} (absent from main.bicepparam)"
        )


def test_template_has_no_hardcoded_identity() -> None:
    """No GUID or repository owner is baked into the template, and every
    identity-bearing parameter is a REPLACE-WITH-* placeholder.
    """
    text = _template_text()
    assert not _GUID_RE.search(text), "template hardcodes a GUID; use a REPLACE-WITH-* placeholder"
    owner = _git_owner()
    if owner:
        assert owner.lower() not in text.lower(), "template hardcodes the repository owner"
    for name in _IDENTITY_PARAMS:
        match = re.search(rf"^\s*param\s+{re.escape(name)}\s*=\s*(.+)$", text, re.M)
        assert match, f"identity parameter {name!r} must be set in the template"
        value = match.group(1)
        assert "REPLACE-WITH" in value, (
            f"identity parameter {name!r} must be a REPLACE-WITH-* placeholder, got {value!r}"
        )


def test_template_documents_out_of_band_inputs() -> None:
    """The header names the values supplied outside the file — the subscription
    and tenant id (deploy-identity OIDC) and the secrets/cert (the Key Vault
    loader) — so the documented set is genuinely complete.
    """
    lowered = _template_text().lower()
    assert "subscription" in lowered and "tenant" in lowered, (
        "template header must name the out-of-band subscription/tenant inputs"
    )
    assert "load-key-vault-secrets.sh" in lowered, (
        "template header must point to the Key Vault loader for the secret/cert inputs"
    )


@pytest.mark.skipif(
    shutil.which("bicep") is None and shutil.which("az") is None,
    reason="bicep/az CLI absent; the infra workflow validate job is authoritative",
)
def test_template_compiles(tmp_path: Path) -> None:
    """The template compiles against the orchestrator with no error (local fast
    check). Built through a sibling ``.bicepparam`` copy so the ``using``
    relative path resolves and the CLI accepts the extension.
    """
    handle, raw = tempfile.mkstemp(dir=str(INFRA_DIR), suffix=".bicepparam")
    tmp = Path(raw)
    import os

    os.close(handle)
    try:
        tmp.write_text(_template_text(), encoding="utf-8")
        outfile = tmp_path / "params.json"
        if shutil.which("bicep") is not None:
            cmd = ["bicep", "build-params", str(tmp), "--outfile", str(outfile)]
        else:
            cmd = ["az", "bicep", "build-params", "--file", str(tmp), "--outfile", str(outfile)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        assert proc.returncode == 0, f"bicep build-params failed:\n{proc.stderr}\n{proc.stdout}"
    finally:
        tmp.unlink(missing_ok=True)
