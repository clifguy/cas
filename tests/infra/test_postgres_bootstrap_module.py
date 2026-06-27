"""Structural gate for the in-VNet Postgres bootstrap job module.

Locks the shape of ``infra/modules/postgres-bootstrap.bicep`` — the Container Apps
Job in the CAS cloud deployment profile (CAS-ADR-042) that runs the idempotent
admin-side database bootstrap (managed-identity roles + extensions) from inside the
VNet, since the server has no public endpoint. The job runs as the dedicated
bootstrap identity (the server's Entra administrator), pulls the SAGE image by that
identity, and invokes the codified ``sage.storage.postgres.cloud_bootstrap``
entrypoint. The bootstrap is provisioning-as-code, not a hand-run runbook (CAS
Cloud Deployment Discipline, Principle 3).

These checks read the tracked Bicep text only — they need no Azure or Bicep
tooling, so they run in the ordinary Python test job. The authoritative compile +
lint is the infra workflow's ``validate`` job; a local fast-path compile is
provided here, skipped when neither CLI is present. Detector logic lives in small
pure helpers so the control tests can prove each detector actually fires.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
INFRA_DIR: Final[Path] = REPO_ROOT / "infra"
MAIN_BICEP: Final[Path] = INFRA_DIR / "main.bicep"
MODULE: Final[Path] = INFRA_DIR / "modules" / "postgres-bootstrap.bicep"

# The Container Apps Job resource type the module must declare.
_JOB_TYPE: Final[str] = "Microsoft.App/jobs"
_ROLE_ASSIGNMENT_TYPE: Final[str] = "Microsoft.Authorization/roleAssignments"

# The codified bootstrap entrypoint the job invokes.
_ENTRYPOINT: Final[str] = "sage.storage.postgres.cloud_bootstrap"

# Built-in Azure role: AcrPull (data-plane image pull). A fixed, public Azure
# constant — not an identity coordinate — so the GUID gate exempts it.
_ACR_PULL_ROLE: Final[str] = "7f951dda-4ed3-4680-a7ca-43fe172d538d"

# Environment variables the job must inject for the bootstrap entrypoint.
_REQUIRED_ENV: Final[tuple[str, ...]] = (
    "AZURE_CLIENT_ID",
    "PG_FQDN",
    "PG_ADMIN_USER",
    "SAGE_DB_ROLE",
    "BFF_DB_ROLE",
)

# A subscription / tenant / object id is a GUID; none may be hardcoded into the
# module — identities arrive as parameters. The public role-definition GUID above
# is the one allowed exception.
_GUID_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b"
)


# ---------------------------------------------------------------------------
# Detectors (pure text functions — exercised by the control tests below)
# ---------------------------------------------------------------------------


def _strip_line_comments(text: str) -> str:
    """Return ``text`` with ``//`` line comments removed."""
    return "\n".join(re.sub(r"//.*$", "", line) for line in text.splitlines())


def _declares_resource_type(text: str, resource_type: str) -> bool:
    """True iff ``text`` declares a resource of ``resource_type`` (not a comment)."""
    pattern = re.compile(r"resource\s+\w+\s+'" + re.escape(resource_type) + r"@[0-9A-Za-z-]+'")
    return pattern.search(_strip_line_comments(text)) is not None


def _module_text() -> str:
    return MODULE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Structural gates
# ---------------------------------------------------------------------------


def test_module_exists() -> None:
    """The bootstrap-job module the orchestrator wires must exist."""
    assert MODULE.is_file(), "infra/modules/postgres-bootstrap.bicep missing"


def test_declares_container_apps_job() -> None:
    """The module declares a Container Apps Job — the in-VNet reach mechanism."""
    assert _declares_resource_type(_module_text(), _JOB_TYPE), (
        f"postgres-bootstrap.bicep must declare a {_JOB_TYPE} resource"
    )


def test_job_runs_in_aca_environment() -> None:
    """The job binds the ACA environment so it runs VNet-integrated and can reach
    the private Postgres subnet.
    """
    text = _strip_line_comments(_module_text())
    assert "environmentId" in text, "the job must bind environmentId (ACA environment)"
    assert "acaEnvironmentId" in text, "the job must consume the acaEnvironmentId parameter"


def test_job_uses_user_assigned_bootstrap_identity() -> None:
    """The job runs as a user-assigned managed identity — the bootstrap identity,
    which is the server's Entra administrator.
    """
    text = _strip_line_comments(_module_text())
    assert re.search(r"type:\s*'UserAssigned'", text), (
        "the job must run as a UserAssigned managed identity"
    )
    assert "bootstrapIdentityId" in text, "the job must attach the bootstrap identity parameter"


def test_job_pulls_image_via_identity() -> None:
    """The job pulls its image by managed identity (AcrPull), never a stored
    registry credential.
    """
    text = _strip_line_comments(_module_text())
    assert "registries" in text, "the job must declare a registries block for the image pull"
    assert re.search(r"identity:\s*\w", text), "the registry pull must reference an identity"
    lowered = text.lower()
    assert "passwordsecretref" not in lowered, "registry must not use a stored credential"
    assert "administratorlogin" not in lowered, "the job must not declare an admin login"


def test_job_invokes_codified_entrypoint() -> None:
    """The job invokes the committed, unit-tested bootstrap module rather than ad
    hoc inline SQL.
    """
    assert _ENTRYPOINT in _module_text(), (
        f"the job command must invoke the codified entrypoint {_ENTRYPOINT!r}"
    )


def test_job_passes_required_env() -> None:
    """The job injects the coordinates the bootstrap entrypoint resolves from the
    environment — the admin identity selector, the server, and the role names.
    """
    text = _module_text()
    for name in _REQUIRED_ENV:
        assert f"'{name}'" in text, f"the job must inject the {name!r} environment variable"


def test_manual_trigger() -> None:
    """The job is manually triggered — declared by the deploy, started on bring-up
    (the operator doc records the trigger; CI orchestration is a later concern).
    """
    assert re.search(r"triggerType:\s*'Manual'", _strip_line_comments(_module_text())), (
        "the job must declare triggerType: 'Manual'"
    )


def test_grants_acrpull_to_bootstrap_identity() -> None:
    """The bootstrap identity is granted AcrPull on the registry so the job can
    pull the SAGE image.
    """
    text = _strip_line_comments(_module_text())
    assert _declares_resource_type(text, _ROLE_ASSIGNMENT_TYPE), (
        f"the module must declare a {_ROLE_ASSIGNMENT_TYPE} (AcrPull) resource"
    )
    assert _ACR_PULL_ROLE in text, "the role assignment must reference the AcrPull role id"
    assert "bootstrapIdentityPrincipalId" in text, (
        "AcrPull must be granted to the bootstrap identity principal"
    )


def test_rg_scoped() -> None:
    """The module is resource-group scoped (the Bicep default)."""
    text = _strip_line_comments(_module_text())
    assert not re.search(r"targetScope\s*=\s*'(subscription|managementGroup|tenant)'", text), (
        "postgres-bootstrap.bicep is a resource-group module; it must not retarget the scope"
    )


def test_no_hardcoded_identity() -> None:
    """No identity GUID is baked into the module — identities arrive as parameters.
    The public AcrPull role-definition GUID is the one allowed constant.
    """
    text = _module_text().replace(_ACR_PULL_ROLE, "")
    assert not _GUID_RE.search(text), (
        "postgres-bootstrap.bicep must not hardcode an identity GUID "
        "(only the public AcrPull role id is allowed)"
    )


def test_main_wires_bootstrap_module() -> None:
    """The orchestrator wires the bootstrap-job module live, scopes it to the
    resource group, and feeds it the ACA environment, the bootstrap identity, and
    the Postgres server FQDN.
    """
    text = _strip_line_comments(MAIN_BICEP.read_text(encoding="utf-8"))
    assert re.search(r"module\s+\w+\s+'modules/postgres-bootstrap\.bicep'\s*=", text), (
        "main.bicep must wire a live module from modules/postgres-bootstrap.bicep"
    )
    assert "foundation.outputs.acaEnvironmentId" in text, (
        "the bootstrap module must consume the ACA environment id"
    )
    assert "identity.outputs.bootstrapIdentityId" in text, (
        "the bootstrap module must consume the bootstrap identity"
    )
    assert "postgres.outputs.postgresServerFqdn" in text, (
        "the bootstrap module must consume the Postgres server FQDN"
    )


def test_main_wires_bootstrap_identity_as_postgres_admin() -> None:
    """The orchestrator sets the bootstrap identity as the Postgres Entra admin so
    the job's token can administer the database — the codified admin, no deploy-time
    GUID required.
    """
    text = _strip_line_comments(MAIN_BICEP.read_text(encoding="utf-8"))
    # The binding may be a multi-line ternary (operator-supplied override vs the
    # default bootstrap identity), so match across newlines but bound the span so
    # it stays within the aadAdminObjectId assignment.
    assert re.search(
        r"aadAdminObjectId:[\s\S]{0,160}?identity\.outputs\.bootstrapIdentityPrincipalId", text
    ), (
        "main.bicep must wire the postgres module's aadAdminObjectId from the "
        "bootstrap identity principal id"
    )


@pytest.mark.skipif(
    shutil.which("bicep") is None and shutil.which("az") is None,
    reason="bicep/az CLI absent; the infra workflow validate job is authoritative",
)
def test_module_compiles(tmp_path: Path) -> None:
    """The module compiles to ARM JSON with no error (local fast check; the infra
    workflow validate job is the authoritative gate).
    """
    outfile = tmp_path / "postgres-bootstrap.json"
    if shutil.which("bicep") is not None:
        cmd = ["bicep", "build", str(MODULE), "--outfile", str(outfile)]
    else:
        cmd = ["az", "bicep", "build", "--file", str(MODULE), "--outfile", str(outfile)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"bicep build failed:\n{proc.stderr}"


# ---------------------------------------------------------------------------
# Anti-coincidental-pass controls
# ---------------------------------------------------------------------------


def test_resource_type_detector_controls() -> None:
    """``_declares_resource_type`` catches a real job declaration, rejects a comment."""
    declared = "resource job 'Microsoft.App/jobs@2024-03-01' = {\n}\n"
    commented = "// resource job 'Microsoft.App/jobs@2024-03-01' = {\n"
    assert _declares_resource_type(declared, _JOB_TYPE)
    assert not _declares_resource_type(commented, _JOB_TYPE)


def test_comment_stripper_controls() -> None:
    """``_strip_line_comments`` removes a commented declaration, keeps a live one."""
    commented = "  // triggerType: 'Manual'"
    assert "triggerType" not in _strip_line_comments(commented)
    live = "  triggerType: 'Manual'"
    assert "triggerType" in _strip_line_comments(live)


def test_guid_exemption_control() -> None:
    """The GUID gate flags an identity GUID but passes once the public AcrPull role
    id is stripped — proving the exemption is the role id, not a blanket pass.
    """
    leak = "var x = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'\n"
    assert _GUID_RE.search(leak), "GUID detector must flag a literal identity GUID"
    only_role = f"var acrPullRoleId = '{_ACR_PULL_ROLE}'\n".replace(_ACR_PULL_ROLE, "")
    assert not _GUID_RE.search(only_role), "stripping the AcrPull role id must clear the GUID gate"
