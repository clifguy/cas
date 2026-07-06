"""Structural gate for the in-VNet vault-teardown job module.

Locks the shape of ``infra/modules/vault-teardown-job.bicep`` — the Container Apps
Job in the CAS cloud deployment profile (CAS-ADR-043/034) that runs the out-of-band
whole-vault teardown from inside the VNet, since both the Postgres schema and the
SharePoint source tree are reachable only from in-cloud. The job runs as the SAGE
workload identity — the one identity that already holds both grants the teardown
needs (schema owner + SharePoint ``Sites.Selected`` writer), so no new identity and
no new Azure permission are introduced — and invokes the codified
``sage.maintenance.delete_vault_cloud`` entrypoint. The per-invocation request is
injected by the teardown workflow at job-start, never baked into the job.

These checks read the tracked Bicep text only — they need no Azure or Bicep tooling,
so they run in the ordinary Python test job. The authoritative compile + lint is the
infra workflow's ``validate`` job; a local fast-path compile is provided here,
skipped when neither CLI is present.
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
MODULE: Final[Path] = INFRA_DIR / "modules" / "vault-teardown-job.bicep"

_JOB_TYPE: Final[str] = "Microsoft.App/jobs"
_ROLE_ASSIGNMENT_TYPE: Final[str] = "Microsoft.Authorization/roleAssignments"

# The codified teardown entrypoint the job invokes.
_ENTRYPOINT: Final[str] = "sage.maintenance.delete_vault_cloud"

# Standing coordinates the job bakes for the entrypoint's env-driven config.
_REQUIRED_ENV: Final[tuple[str, ...]] = (
    "AZURE_CLIENT_ID",
    "PG_FQDN",
    "PG_DATABASE",
    "PG_USER",
    "SHAREPOINT_SITE_ID",
    "SHAREPOINT_DRIVE_ID",
    "SHAREPOINT_ROOT_PATH",
)

# The per-invocation teardown request must NOT be baked into the deployed job — it
# is supplied by the teardown workflow as job-start env-var overrides, so the job as
# deployed cannot delete anything.
_FORBIDDEN_BAKED_ENV: Final[tuple[str, ...]] = (
    "SAGE_DELETE_VAULT_ID",
    "SAGE_DELETE_CONFIRM",
    "SAGE_DELETE_APPLY",
    "SAGE_DELETE_SNAPSHOT",
)

_GUID_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b"
)


# ---------------------------------------------------------------------------
# Detectors (pure text functions — exercised by the control tests below)
# ---------------------------------------------------------------------------


def _strip_line_comments(text: str) -> str:
    return "\n".join(re.sub(r"//.*$", "", line) for line in text.splitlines())


def _declares_resource_type(text: str, resource_type: str) -> bool:
    pattern = re.compile(r"resource\s+\w+\s+'" + re.escape(resource_type) + r"@[0-9A-Za-z-]+'")
    return pattern.search(_strip_line_comments(text)) is not None


def _module_text() -> str:
    return MODULE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Structural gates
# ---------------------------------------------------------------------------


def test_module_exists() -> None:
    """The teardown-job module the orchestrator wires must exist."""
    assert MODULE.is_file(), "infra/modules/vault-teardown-job.bicep missing"


def test_declares_container_apps_job() -> None:
    """The module declares a Container Apps Job — the in-VNet reach mechanism."""
    assert _declares_resource_type(_module_text(), _JOB_TYPE), (
        f"vault-teardown-job.bicep must declare a {_JOB_TYPE} resource"
    )


def test_job_runs_in_aca_environment() -> None:
    """The job binds the ACA environment so it runs VNet-integrated (reaching the
    private Postgres subnet and Microsoft Graph)."""
    text = _strip_line_comments(_module_text())
    assert "environmentId" in text, "the job must bind environmentId (ACA environment)"
    assert "acaEnvironmentId" in text, "the job must consume the acaEnvironmentId parameter"


def test_job_runs_as_sage_identity_not_bootstrap() -> None:
    """The job runs as the SAGE user-assigned identity — not the bootstrap identity.

    Anti-coincidental-pass: assert the SAGE identity *and* the absence of the
    bootstrap identity. A copy-paste from postgres-bootstrap.bicep would wire the
    bootstrap identity, which holds Postgres admin but not the SharePoint
    Sites.Selected write grant — so the SharePoint folder delete would fail in cloud
    while a naive 'runs as an identity' check still passed.
    """
    text = _strip_line_comments(_module_text())
    assert re.search(r"type:\s*'UserAssigned'", text), (
        "the job must run as a UserAssigned managed identity"
    )
    assert "sageIdentityId" in text, "the job must attach the SAGE identity parameter"
    assert "bootstrapIdentity" not in text, (
        "the teardown job must run as the SAGE identity, never the bootstrap identity"
    )


def test_job_pulls_image_via_identity() -> None:
    """The job pulls its image by managed identity, never a stored credential."""
    text = _strip_line_comments(_module_text())
    assert "registries" in text, "the job must declare a registries block for the image pull"
    assert re.search(r"identity:\s*sageIdentityId", text), (
        "the registry pull must reference the SAGE identity"
    )
    lowered = text.lower()
    assert "passwordsecretref" not in lowered, "registry must not use a stored credential"
    assert "administratorlogin" not in lowered, "the job must not declare an admin login"


def test_does_not_redeclare_acrpull() -> None:
    """The module declares no role assignment: the SAGE identity's AcrPull grant is
    already declared by the container-apps module.

    Anti-coincidental-pass: a re-declared AcrPull with the same deterministic
    ``guid(acr.id, sagePrincipalId, role)`` name would clash with the container-apps
    grant. Asserting the module declares no ``roleAssignments`` at all catches a
    stray copy of the bootstrap module's grant block.
    """
    assert not _declares_resource_type(_module_text(), _ROLE_ASSIGNMENT_TYPE), (
        "the teardown job must not re-declare an AcrPull role assignment; the SAGE "
        "identity's grant is owned by the container-apps module"
    )


def test_job_invokes_codified_entrypoint() -> None:
    """The job invokes the committed, unit-tested cloud teardown entrypoint."""
    assert _ENTRYPOINT in _module_text(), (
        f"the job command must invoke the codified entrypoint {_ENTRYPOINT!r}"
    )


def test_job_bakes_standing_coordinates() -> None:
    """The job injects the standing coordinates the entrypoint's env-driven config
    resolves — the identity selector, the Postgres server/database/role, and the
    SharePoint site/drive/root."""
    text = _module_text()
    for name in _REQUIRED_ENV:
        assert f"'{name}'" in text, f"the job must inject the {name!r} environment variable"


def test_per_invocation_request_not_baked() -> None:
    """The destructive per-invocation request is never baked into the deployed job.

    Anti-coincidental-pass: assert each ``SAGE_DELETE_*`` request variable is absent
    from the module's live text (comments stripped, so the explanatory comment naming
    them is fine). A job that baked the vault id + apply flag would be able to delete a
    vault the moment it started, with no operator confirmation — the request must
    arrive only as a job-start override from the teardown workflow.
    """
    text = _strip_line_comments(_module_text())
    for name in _FORBIDDEN_BAKED_ENV:
        assert name not in text, (
            f"{name!r} must not be baked into the job; it is a job-start override"
        )


def test_manual_trigger() -> None:
    """The job is manually triggered — declared by the deploy, started out-of-band by
    the teardown workflow."""
    assert re.search(r"triggerType:\s*'Manual'", _strip_line_comments(_module_text())), (
        "the job must declare triggerType: 'Manual'"
    )


def test_no_auto_retry() -> None:
    """A destructive job does not auto-retry: re-execution is a deliberate operator
    dispatch (the entrypoint is idempotent, so a resumed run is safe, but not silent).
    """
    assert re.search(r"replicaRetryLimit:\s*0", _strip_line_comments(_module_text())), (
        "the teardown job must set replicaRetryLimit: 0 (no silent re-execution)"
    )


def test_rg_scoped() -> None:
    """The module is resource-group scoped (the Bicep default)."""
    text = _strip_line_comments(_module_text())
    assert not re.search(r"targetScope\s*=\s*'(subscription|managementGroup|tenant)'", text), (
        "vault-teardown-job.bicep is a resource-group module; it must not retarget the scope"
    )


def test_no_hardcoded_identity() -> None:
    """No identity GUID is baked into the module — identities arrive as parameters.
    The teardown module declares no role assignment, so no role-id constant is
    exempt: any GUID at all is a leak.
    """
    assert not _GUID_RE.search(_module_text()), (
        "vault-teardown-job.bicep must not hardcode a GUID (identities arrive as parameters)"
    )


def test_main_wires_teardown_module() -> None:
    """The orchestrator wires the teardown-job module live, feeds it the ACA
    environment, the SAGE identity, the Postgres server FQDN, and the SharePoint
    coordinates, and exposes the job name as an output for the workflow to start.
    """
    text = _strip_line_comments(MAIN_BICEP.read_text(encoding="utf-8"))
    assert re.search(r"module\s+\w+\s+'modules/vault-teardown-job\.bicep'\s*=", text), (
        "main.bicep must wire a live module from modules/vault-teardown-job.bicep"
    )
    assert "identity.outputs.sageIdentityId" in text, (
        "the teardown module must consume the SAGE identity"
    )
    assert "postgres.outputs.postgresServerFqdn" in text, (
        "the teardown module must consume the Postgres server FQDN"
    )
    assert "sharepointSiteId: sharepointSiteId" in text, (
        "the teardown module must consume the SharePoint site id"
    )
    assert (
        "output vaultTeardownJobName string = vaultTeardown.outputs.vaultTeardownJobName" in text
    ), "main.bicep must output the teardown job name for the workflow to start"


@pytest.mark.skipif(
    shutil.which("bicep") is None and shutil.which("az") is None,
    reason="bicep/az CLI absent; the infra workflow validate job is authoritative",
)
def test_module_compiles(tmp_path: Path) -> None:
    """The module compiles to ARM JSON with no error (local fast check; the infra
    workflow validate job is the authoritative gate)."""
    outfile = tmp_path / "vault-teardown-job.json"
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
    """``_declares_resource_type`` catches a real declaration, rejects a comment."""
    declared = "resource job 'Microsoft.App/jobs@2024-03-01' = {\n}\n"
    commented = "// resource job 'Microsoft.App/jobs@2024-03-01' = {\n"
    assert _declares_resource_type(declared, _JOB_TYPE)
    assert not _declares_resource_type(commented, _JOB_TYPE)


def test_role_assignment_detector_control() -> None:
    """The role-assignment detector fires on a real declaration — so
    ``test_does_not_redeclare_acrpull`` is a live gate, not a vacuous pass."""
    declared = "resource g 'Microsoft.Authorization/roleAssignments@2022-04-01' = {\n}\n"
    assert _declares_resource_type(declared, _ROLE_ASSIGNMENT_TYPE)
    assert not _declares_resource_type("// no role here\n", _ROLE_ASSIGNMENT_TYPE)


def test_guid_detector_control() -> None:
    """The GUID gate flags a literal GUID (so ``test_no_hardcoded_identity`` is live)."""
    assert _GUID_RE.search("var x = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'\n")
    assert not _GUID_RE.search("var x = 'not-a-guid'\n")
