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
    except subprocess.CalledProcessError, FileNotFoundError:
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


def test_runbook_describes_the_audit_as_scoped_in_work_as_well_as_verdict() -> None:
    """The source-file audit step says the check is scoped to the run's probes,
    and no longer warns that the audit's cost grows with accumulated residue.

    The driver passes its probe ids as the audit's ``document_ids`` scope, so the
    walk is bounded by what the run created. A runbook still carrying the old
    warning would send an operator counting documents to explain a slow check
    that can no longer be caused that way.
    """
    text = _runbook_text()
    assert "document_ids" in text, "runbook must name the scope the audit is called with"
    for stale in ("Residue grows", "the audit's cost grows with it"):
        assert stale not in text, f"runbook still warns that residue grows the audit: {stale!r}"


def test_operator_command_blocks_declare_the_rewrite_expectation() -> None:
    """Every runbook invocation of the driver passes ``--expect-rewritten``.

    The runbook drives a tenant-backed vault, where several of the provenance
    check's assertions only bite if the store actually rewrote the retained copy.
    Left at the ``any`` default, a run against a store that had stopped rewriting
    reports ``rewritten=no`` and PASSes — unenforced and green, which is the state
    the expectation exists to prevent.

    Pinned here because its absence is why it went missing: the CI harness gained
    the flag and a gate to hold it, while the runbook — the path used for exactly
    the ad-hoc investigation where the answer matters most — kept the older
    command and nothing noticed.
    """
    # Anchored on the command form, not on the filename: the runbook also *names*
    # the driver in prose, and a gate that could not tell a sentence from a
    # command would fire on the wrong line — the same conflation of prose with
    # executable instruction that this test exists to guard against. `--phase` is
    # required by the driver, so every real invocation carries it.
    invocations = [
        stripped
        for line in _runbook_text().splitlines()
        if (stripped := line.strip()).startswith("python3 ")
        and "sharepoint_validate.py" in stripped
        and "--phase" in stripped
    ]
    assert len(invocations) >= 2, (
        f"expected the pre-restart and post-restart invocations, found {invocations}"
    )
    for line in invocations:
        assert "--expect-rewritten" in line, (
            "every runbook driver invocation must state the rewrite expectation, or an "
            f"operator copying it verbatim runs unenforced: {line!r}"
        )


SEED_SCRIPT: Final[Path] = REPO_ROOT / "deploy" / "bootstrap" / "seed-vault-source.sh"
_ASSIGNMENT_RE: Final[re.Pattern[str]] = re.compile(r'^(\w+)="\$\(', re.MULTILINE)
_POST_RE: Final[re.Pattern[str]] = re.compile(r"az rest --method POST\b")
# An option the commands are compared on, with its value: a double-quoted
# string (escaped quotes included), a single-quoted string, or a bare word.
_OPTION_RE: Final[re.Pattern[str]] = re.compile(
    r"--(uri|filter|query|body) (\"(?:[^\"\\]|\\.)*\"|'[^']*'|[^\s)\"]+)"
)


def _options(text: str, start: int, end: int) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in _OPTION_RE.finditer(text, start, end)}


def _lookups(text: str) -> dict[str, dict[str, str]]:
    """Map each variable assigned from a command substitution to its command's options.

    Keyed by the variable, so a value is compared with the one the other file
    uses for the same lookup -- containment anywhere in the file would let one
    command's value satisfy another's.
    """
    starts = list(_ASSIGNMENT_RE.finditer(text))
    out = {}
    for n, match in enumerate(starts):
        end = starts[n + 1].start() if n + 1 < len(starts) else len(text)
        end = min(end, text.find(')"', match.end()) + 2 or end)
        options = _options(text, match.end(), end)
        if options:
            out[match.group(1)] = options
    return out


def _posts(text: str) -> list[dict[str, str]]:
    """The ``--uri`` and ``--body`` of each POST, in order."""
    out = []
    for match in _POST_RE.finditer(text):
        end = text.find("\n\n", match.end())
        out.append(_options(text, match.end(), end if end != -1 else len(text)))
    return out


def test_runbook_lookups_mirror_the_seed_script() -> None:
    """The runbook shows the commands the seed script runs.

    Each value the script resolves through a command substitution is resolved
    in the runbook, into the same variable, by a command with the same URI,
    filter and query; and each grant is posted to the same URI with the same
    body. So the exact-match selection, the lookup-before-grant checks and the
    grants themselves cannot drift apart between the two. The prefix-matching
    ``--display-name`` lookup and a ``|| true`` that would hide a refused grant
    appear in neither.
    """
    script, runbook = SEED_SCRIPT.read_text(), _runbook_text()
    expected = _lookups(script)
    assert {"GRAPH_SP_ID", "SITE_ID", "existing_role", "existing_grant"} <= expected.keys()
    assert _lookups(runbook) == expected
    script_posts = _posts(script)
    assert len(script_posts) == 2 and all({"uri", "body"} <= p.keys() for p in script_posts)
    assert _posts(runbook) == script_posts
    for text, name in ((script, "script"), (runbook, "runbook")):
        # Anchored on the flag taking a value: both files name it in prose to
        # say why it is not used.
        assert not re.search(r"--display-name\s+['\"]", text), f"{name} selects by prefix"
        assert "|| true" not in text, f"{name} swallows a failed call"


_SITES_AUTH: Final[str] = '--headers "Authorization=@${graph_auth}"'
_AZ_REST_RE: Final[re.Pattern[str]] = re.compile(r"\baz rest\b")


def _rest_commands(text: str) -> list[str]:
    """Each ``az rest`` command, up to the next command or blank line."""
    starts = [m.start() for m in _AZ_REST_RE.finditer(text)]
    out = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(text)
        blank = text.find("\n\n", start)
        out.append(text[start : min(end, blank) if blank != -1 else end])
    return out


def _is_site_step(command: str) -> bool:
    return "/permissions" in command or ":/content" in command


def test_site_steps_carry_the_full_control_token_in_both_files() -> None:
    """Steps 3 and 4 run on the Sites.FullControl.All token, in the script and
    in the runbook alike, and no other call does.

    Graph admits the site-permission calls only for a delegated token carrying
    that scope, which the Azure CLI's own client can never obtain; the token
    reaches ``az`` through a private file rather than the command line. Both
    files mint it with the same helper against the same signed-in tenant.
    """
    script, runbook = SEED_SCRIPT.read_text(), _runbook_text()
    for text, name in ((script, "script"), (runbook, "runbook")):
        commands = _rest_commands(text)
        site = [c for c in commands if _is_site_step(c)]
        assert len(site) >= 3, f"{name}: expected the permission GET/POST and the upload"
        for command in site:
            assert _SITES_AUTH in command, f"{name}: a site step lacks the token: {command!r}"
        for command in commands:
            if not _is_site_step(command) and "/drives/" not in command:
                assert "Authorization" not in command, f"{name}: {command!r}"
        assert "az account show --query tenantId" in text, name
        assert re.search(r'graph_sites_token\.py"? mint --tenant "\$\{TENANT_ID\}"', text), name
        assert "Authorization=Bearer" not in text, f"{name} puts the token on a command line"


def test_runbook_states_the_privilege_the_site_steps_need() -> None:
    """The privilege section names what Graph actually requires for steps 3 and
    4 -- the delegated Sites.FullControl.All scope held by a SharePoint
    Administrator or higher, through a client that can carry it -- and no
    longer claims a site owner or member suffices.
    """
    text = _runbook_text()
    for needed in ("Sites.FullControl.All", "SharePoint Administrator", "graph_sites_token.py"):
        assert needed in text, f"runbook must name {needed}"
    assert "AADSTS65002" in text, "runbook must say why the Azure CLI client cannot be used"
    for stale in ("site owner", "owner / member"):
        assert stale not in text, f"runbook still claims {stale!r} suffices"
