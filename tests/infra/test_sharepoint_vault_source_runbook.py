"""Structural and identity-hygiene gate for the SharePoint vault-source runbook.

Locks the shape of ``docs/process/sharepoint-vault-source.md`` -- the one-time,
hand-run procedure that grants the SAGE managed identity the least-privilege,
site-scoped Microsoft Graph permission backing the cloud document-store
vault-source binding (CAS-ADR-043), and resolves the site/library coordinates the
deployment threads into the SAGE cloud config.

These checks read the tracked runbook only -- they need no Azure tooling and no
live tenant -- so they run in the ordinary Python test job alongside the other
infra gates. Like those gates, identity coordinates (subscription, tenant,
client, and application ids) may never be hardcoded: they are resolved at run time
into shell variables.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
RUNBOOK: Final[Path] = REPO_ROOT / "docs" / "process" / "sharepoint-vault-source.md"

# A subscription / tenant / client / application id is a GUID; none may be
# hardcoded into the runbook -- they arrive resolved into shell variables.
_GUID_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b"
)


def _git_owner() -> str | None:
    """Derive the repository owner from the origin remote, or ``None``.

    Resolved at run time so this durable surface carries no personal-identity
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


def test_runbook_exists() -> None:
    """The runbook is the shippable, reviewable artifact for the Graph grant --
    the procedure exists in the repo even though the directory objects are created
    by a one-time hand run against the tenant.
    """
    assert RUNBOOK.is_file(), "docs/process/sharepoint-vault-source.md missing"


def test_runbook_records_chosen_approach() -> None:
    """The provisioning approach is on the record: a scripted ``az`` / Microsoft
    Graph procedure, with the Microsoft Graph Bicep extension named as the
    alternative that was considered and not adopted.
    """
    text = _runbook_text()
    assert "az rest" in text, "runbook must document the scripted `az rest` procedure"
    assert "Microsoft Graph Bicep extension" in text, (
        "runbook must record the Microsoft Graph Bicep extension as the "
        "alternative that was considered and not adopted"
    )
    assert "not adopted" in text.lower(), (
        "runbook must state the Bicep-extension alternative was not adopted"
    )


def test_least_privilege_site_scoped_grant_documented() -> None:
    """The grant is least-privilege and site-scoped: the runbook names the
    ``Sites.Selected`` application role, the per-site permission that scopes it to
    a single site, and excludes the tenant-wide alternative.
    """
    text = _runbook_text()
    lowered = text.lower()
    assert "Sites.Selected" in text, "runbook must name the Sites.Selected application role"
    assert "least privilege" in lowered or "least-privilege" in lowered, (
        "runbook must state the grant is least-privilege"
    )
    assert "single site" in lowered, "runbook must scope the grant to a single site"
    assert "Sites.ReadWrite.All" in text, (
        "runbook must name the tenant-wide alternative it deliberately avoids"
    )


def test_coordinates_threaded_into_iac_documented() -> None:
    """The runbook resolves the site and library ids the deployment needs and
    names the Bicep params they feed.
    """
    text = _runbook_text()
    assert "sharepointSiteId" in text, "runbook must name the sharepointSiteId Bicep param"
    assert "sharepointDriveId" in text, "runbook must name the sharepointDriveId Bicep param"
    assert "/drives" in text, "runbook must resolve the document-library drive id"


def test_runbook_no_hardcoded_identity() -> None:
    """No subscription/tenant/client/application GUID or repository owner is baked
    into the runbook -- identity is resolved at run time into shell variables.
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


def test_live_validation_section_documented() -> None:
    """The runbook captures the live end-to-end validation procedure: the driver
    that exercises ingest/readback/audit through the edge, both phases either side
    of a container restart, the source-file integrity audit, and the
    least-privilege probe against an un-granted site (named by a resolved variable
    so ``test_runbook_no_hardcoded_identity`` still holds).
    """
    text = _runbook_text()
    lowered = text.lower()
    assert "live end-to-end validation" in lowered, (
        "runbook must document the live end-to-end validation procedure"
    )
    assert "sharepoint_validate.py" in text, "runbook must name the validation driver"
    assert "pre-restart" in text and "post-restart" in text, (
        "runbook must run the validation either side of a container restart"
    )
    assert "restart" in lowered, "runbook must document the restart-survival step"
    assert "$UNGRANTED_SITE_ID" in text, (
        "runbook must probe an un-granted site (resolved variable) for the least-privilege check"
    )
    assert "verify_vault_source_files" in text or "verify-source-files" in text, (
        "runbook must name the source-file integrity audit"
    )
