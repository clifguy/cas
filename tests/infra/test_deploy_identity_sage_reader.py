"""Structural and identity-hygiene gate for the deploy-identity Sage.Reader grant.

Locks the section of ``docs/process/azure-deployment.md`` that grants the
GitHub-OIDC CI deploy service principal the least-privilege ``Sage.Reader``
application role on the SAGE resource server. Without it the post-deploy
preflight's v2 ``.default`` token clears the APIM edge (which validates only
issuer + audience) but is rejected by the SAGE backend, which requires at least
one configured role or scope -- a daemon principal carries the ``roles`` claim,
never a delegated scope, so the read-only ``Sage.Reader`` role is the necessary
and minimal grant (CAS-ADR-042).

Modeled on ``test_sharepoint_vault_source_runbook.py`` -- the analogous gate for
a least-privilege app-role grant runbook. These checks read the tracked runbook
only; they need no Azure tooling and no live tenant, so they run in the ordinary
Python test job alongside the other infra gates. The grant assertions are scoped
to the grant subsection so they cannot pass coincidentally on the unrelated
"least-privilege" sentence the runbook already carries for the subscription-scope
role pair. Identity coordinates may never be hardcoded: the deploy and SAGE
service-principal object ids and the role id are resolved at run time into shell
variables.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
RUNBOOK: Final[Path] = REPO_ROOT / "docs" / "process" / "azure-deployment.md"

# The heading that opens the grant subsection. The assertions below are scoped to
# the text between this heading and the next same-or-higher-level heading, so a
# match cannot be satisfied by the runbook's pre-existing "least-privilege"
# sentence about the Contributor / User Access Administrator pair.
_GRANT_HEADING: Final[str] = "Grant the deploy identity read on SAGE"

# A subscription / tenant / client / object id is a GUID; none may be hardcoded
# into the runbook -- they arrive resolved into shell variables at run time.
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


def _grant_section() -> str:
    """Return the grant subsection text (its heading line to the next heading).

    Isolating the subsection is what makes the grant assertions
    anti-coincidental: the runbook already says "least-privilege" elsewhere (for
    the subscription-scope role pair), so an unscoped scan would pass on the
    unmodified file. An empty string is returned when the heading is absent, so
    the callers' assertions fail loudly before the section exists.
    """
    text = _runbook_text()
    lines = text.splitlines(keepends=True)
    start: int | None = None
    start_level = 0
    for i, line in enumerate(lines):
        heading = re.match(r"^(#{1,6})\s", line)
        if start is None:
            if heading and _GRANT_HEADING in line:
                start = i
                start_level = len(heading.group(1))
            continue
        # Stop at the next heading of the same or a higher level.
        if heading and len(heading.group(1)) <= start_level:
            return "".join(lines[start:i])
    if start is None:
        return ""
    return "".join(lines[start:])


def test_runbook_grants_deploy_identity_sage_reader() -> None:
    """The runbook codifies the app-role grant of ``Sage.Reader`` to the deploy
    identity, anchored on the command lines that perform it -- not prose. The
    grant is an ``az rest`` POST to the deploy SP's ``appRoleAssignments``, with
    the role id resolved by value and the assignment body carrying the
    ``principalId`` / ``resourceId`` / ``appRoleId`` triple.
    """
    text = _runbook_text()
    uri_lines = [line for line in text.splitlines() if "--uri" in line]
    body_lines = [line for line in text.splitlines() if "--body" in line]

    assert any("appRoleAssignments" in line for line in uri_lines), (
        "runbook must POST the grant to the deploy SP's appRoleAssignments endpoint "
        "(anchored on the --uri command line, not prose)"
    )
    assert "appRoles[?value=='Sage.Reader']" in text, (
        "runbook must resolve the Sage.Reader role id by value from the SAGE service "
        "principal (a command-line anchor, not a prose mention of the role)"
    )
    for field in ("principalId", "resourceId", "appRoleId"):
        assert any(field in line for line in body_lines), (
            f"the appRoleAssignment body must carry {field} (anchored on the --body line)"
        )


def test_sage_reader_grant_is_least_privilege() -> None:
    """The grant subsection states the least-privilege rationale on its own: the
    read-only ``Sage.Reader`` role is the minimal grant the preflight read probe
    needs. Scoped to the subsection so it cannot pass on the runbook's existing
    "least-privilege" sentence about the subscription-scope role pair.
    """
    section = _grant_section()
    assert section, f"runbook must contain a '### ... {_GRANT_HEADING}' subsection"
    lowered = section.lower()
    assert "least privilege" in lowered or "least-privilege" in lowered, (
        "the grant subsection must state the grant is least-privilege"
    )
    assert "read-only" in lowered, (
        "the grant subsection must state Sage.Reader is the read-only / minimal role"
    )
    assert "Sage.Reader" in section, "the grant subsection must name the Sage.Reader role it grants"


def test_sage_reader_grant_resolves_coordinates_at_runtime() -> None:
    """No deploy/SAGE service-principal object id or role id is baked into the
    grant subsection -- every coordinate is resolved at run time into a shell
    variable. (Parallels the global infra-surface gate, scoped to this section.)
    """
    section = _grant_section()
    assert section, f"runbook must contain a '### ... {_GRANT_HEADING}' subsection"
    assert not _GUID_RE.search(section), (
        "grant subsection hardcodes a GUID; resolve identity into a shell variable instead"
    )
    owner = _git_owner()
    if owner:
        assert owner.lower() not in section.lower(), (
            "grant subsection hardcodes the repository owner; use a resolved variable"
        )


def test_guid_regex_control() -> None:
    """Control for the negative GUID assert: the detector matches a real GUID and
    rejects a non-GUID, so ``test_sage_reader_grant_resolves_coordinates_at_runtime``
    cannot pass vacuously.
    """
    assert _GUID_RE.search("12345678-90ab-cdef-1234-567890abcdef")
    assert not _GUID_RE.search("sage-resource-server is not a guid")
