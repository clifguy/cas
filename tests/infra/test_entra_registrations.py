"""Structural and identity-hygiene gate for the Entra app-registration runbook.

Locks the shape of ``docs/process/entra-app-registrations.md`` — the one-time,
hand-run procedure that provisions the two Microsoft Entra app registrations the
cloud authentication model depends on: SAGE as an OAuth resource server, and the
CAS backend-for-frontend as a confidential client that calls SAGE on-behalf-of an
interactive user. The cloud auth model these registrations realize is recorded in
CAS-ADR-042; its concrete binding roster lives in the SAGE Deployment Profile
Bindings steering document.

These checks read the tracked runbook only — they need no Azure tooling and no
live tenant — so they run in the ordinary Python test job alongside the existing
infra scaffolding gate. Like that gate, identity coordinates (subscription,
tenant, client, and application ids) may never be hardcoded: they are resolved at
runtime into shell variables, and the SAGE identifier URI and the BFF redirect
URIs are templated placeholders, never literals.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
RUNBOOK: Final[Path] = REPO_ROOT / "docs" / "process" / "entra-app-registrations.md"

# A subscription / tenant / client / application id is a GUID. None of these
# identity coordinates may be hardcoded into the runbook — they arrive resolved
# into shell variables at run time.
_GUID_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b"
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


def _runbook_text() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def test_entra_runbook_exists() -> None:
    """The runbook is the shippable, reviewable artifact for the registration
    bootstrap — the procedure exists in the repo even though the directory
    objects are created by a one-time hand run against the tenant.
    """
    assert RUNBOOK.is_file(), "docs/process/entra-app-registrations.md missing"


def test_runbook_records_chosen_approach() -> None:
    """The provisioning approach is on the record: a scripted ``az``/Microsoft
    Graph procedure, with the Microsoft Graph Bicep extension named as the
    alternative that was considered and not adopted.
    """
    text = _runbook_text()
    assert "az ad" in text, "runbook must document the scripted `az ad` procedure"
    assert "Microsoft Graph Bicep extension" in text, (
        "runbook must record the Microsoft Graph Bicep extension as the "
        "alternative that was considered and not adopted"
    )


def test_sage_resource_server_documented() -> None:
    """The SAGE resource-server registration declares an ``api://`` application
    ID URI and exposes OAuth2 permission scopes and app roles, and the scope/role
    design is stated to cover both the REST and MCP surfaces.
    """
    text = _runbook_text()
    assert "api://" in text, "SAGE registration must declare an api:// application ID URI"
    assert "oauth2PermissionScopes" in text, (
        "SAGE registration must expose OAuth2 permission scopes"
    )
    assert "appRoles" in text, "SAGE registration must declare app roles"
    assert "REST" in text and "MCP" in text, (
        "the scope/role design must state coverage of both the REST and MCP surfaces"
    )


def test_runbook_records_v2_access_token_version() -> None:
    """The runbook records that the SAGE app registration is set to access-token
    **v2** (``requestedAccessTokenVersion: 2``), so an operator following it — or a
    re-provision — does not regress the token version APIM and the SAGE backend
    require. Mirrors the codified ``deploy/bootstrap/entra-app-registrations.sh``.
    """
    text = _runbook_text()
    assert "requestedAccessTokenVersion" in text, (
        "runbook must document the requestedAccessTokenVersion setting"
    )
    # Same JSON ``key: 2`` anchor as the bootstrap-script gate, so doc and script
    # cannot drift to documented-but-not-codified (or the reverse).
    assert re.search(r'requestedAccessTokenVersion\\?"\s*:\s*2', text), (
        'runbook must show "requestedAccessTokenVersion": 2 in the SAGE api PATCH body'
    )


def test_bff_confidential_client_documented() -> None:
    """The CAS BFF confidential-client registration configures redirect URIs,
    grants the API permission onto the SAGE resource server, and enables the
    on-behalf-of flow.
    """
    text = _runbook_text()
    lowered = text.lower()
    assert "redirect" in lowered, "BFF registration must configure redirect URIs"
    assert ("requiredResourceAccess" in text) or ("permission add" in lowered), (
        "BFF registration must grant the API permission onto the SAGE resource server"
    )
    assert ("on-behalf-of" in lowered) or ("obo" in lowered), (
        "BFF registration must enable the on-behalf-of (OBO) flow"
    )


def test_uniform_single_issuer_documented() -> None:
    """The runbook states the uniform-authorization model: one Entra issuer, one
    resource-server audience, the same token honored across the REST and MCP
    surfaces.
    """
    text = _runbook_text()
    lowered = text.lower()
    assert "single issuer" in lowered, "runbook must state the single-issuer model"
    assert "uniform" in lowered, "runbook must state authorization is uniform across surfaces"
    assert "REST" in text and "MCP" in text, (
        "the uniform-authorization statement must name both surfaces"
    )


def test_entra_runbook_no_hardcoded_identity() -> None:
    """No subscription/tenant/client/application GUID or repository owner is baked
    into the runbook — identity is resolved at runtime into shell variables.
    """
    text = _runbook_text()
    assert not _GUID_RE.search(text), (
        "runbook hardcodes a GUID; resolve identity into a shell variable instead"
    )
    owner = _git_owner()
    if owner:
        assert owner.lower() not in text.lower(), (
            "runbook hardcodes the repository owner; use a resolved variable"
        )


def test_registration_urls_are_templated() -> None:
    """Every ``api://`` identifier URI and every redirect URI is a placeholder —
    a ``<...>`` or ``${...}`` segment — never a literal host or GUID.

    Redirect-URI templating is scoped to lines that mention a redirect so that
    well-known Microsoft service endpoints (``graph.microsoft.com``,
    ``login.microsoftonline.com``) referenced elsewhere are not falsely flagged.
    """
    text = _runbook_text()

    for match in re.finditer(r"api://(\S+)", text):
        body = match.group(1).rstrip("`\"',.)")
        assert "<" in body or "${" in body, (
            f"api:// identifier URI must be templated, not literal: {match.group(0)!r}"
        )

    for line in text.splitlines():
        if "redirect" not in line.lower():
            continue
        for match in re.finditer(r"https://([^\s/`\"']+)", line):
            host = match.group(1)
            assert "<" in host or "${" in host, (
                f"redirect URI host must be templated, not literal: {match.group(0)!r}"
            )
