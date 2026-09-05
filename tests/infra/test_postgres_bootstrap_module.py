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

# Environment variables the job must inject for the bootstrap entrypoint. The
# database name (``PG_DATABASE``) is load-bearing: the extensions are pre-created
# in exactly that database, so it must be the same database the workload connects
# to (see ``test_bootstrap_and_app_share_one_database_name``).
_REQUIRED_ENV: Final[tuple[str, ...]] = (
    "AZURE_CLIENT_ID",
    "PG_FQDN",
    "PG_DATABASE",
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


def _resource_block(text: str, symbol: str) -> str:
    """Return the body of the ``resource <symbol> '...' = {...}`` declaration.

    Slices to the next top-level declaration. The module declares the registry as
    ``existing``, the AcrPull grant, and the job, so a property asserted over the
    whole module can be satisfied by the wrong resource — the job's identity
    binding and the registry pull's are both spelled ``identity:``. Returns ``""``
    when the symbol is not declared.
    """
    stripped = _strip_line_comments(text)
    start = re.search(rf"^resource\s+{re.escape(symbol)}\b", stripped, re.MULTILINE)
    if start is None:
        return ""
    rest = stripped[start.end() :]
    nxt = re.search(r"^(?:resource|output|module|param|var)\s+\w+", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def _job_block() -> str:
    """The bootstrap job's own resource body, from the tracked module."""
    block = _resource_block(_module_text(), "bootstrapJob")
    assert block, "postgres-bootstrap.bicep must declare the bootstrapJob resource"
    return block


def _module_block(text: str, module_file: str) -> str:
    """Return the body of the ``module <symbol> 'modules/<module_file>' = {...}`` call.

    Spans from the module declaration to the next top-level declaration, so an
    assertion can be scoped to one module's parameter block rather than the whole
    orchestrator. The orchestrator hands the ACA environment and the Postgres
    server FQDN to more than one module, so an assertion made over the whole file
    is satisfied by a neighbour. Truncating on ``output`` as well as ``module``
    matters for the last module wired, whose block would otherwise run into the
    orchestrator's own outputs. Returns ``""`` when no module wires that path.
    """
    stripped = _strip_line_comments(text)
    start = re.search(
        r"^module\s+\w+\s+'modules/" + re.escape(module_file) + r"'\s*=", stripped, re.MULTILINE
    )
    if start is None:
        return ""
    rest = stripped[start.end() :]
    nxt = re.search(r"^(?:resource|output|module|param|var)\s+\w+", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def _main_module_block(module_file: str) -> str:
    """:func:`_module_block` over the tracked ``main.bicep``."""
    block = _module_block(MAIN_BICEP.read_text(encoding="utf-8"), module_file)
    assert block, f"main.bicep must wire modules/{module_file}"
    return block


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

    Read out of the job's own body: the module also declares the registry and the
    AcrPull grant, so a property found anywhere in it says nothing about which
    resource carries it.
    """
    job = _job_block()
    assert "environmentId" in job, "the job must bind environmentId (ACA environment)"
    assert "acaEnvironmentId" in job, "the job must consume the acaEnvironmentId parameter"


def test_job_uses_user_assigned_bootstrap_identity() -> None:
    """The job runs as a user-assigned managed identity — the bootstrap identity,
    which is the server's Entra administrator. Read out of the job's own body.
    """
    job = _job_block()
    assert re.search(r"type:\s*'UserAssigned'", job), (
        "the job must run as a UserAssigned managed identity"
    )
    assert "bootstrapIdentityId" in job, "the job must attach the bootstrap identity parameter"


def test_job_pulls_image_via_identity() -> None:
    """The job pulls its image by managed identity (AcrPull), never a stored
    registry credential.

    The positive claims are read out of the job's own body. ``identity:`` appears
    twice in this module — the job's own identity block and the registry pull's
    binding — so a whole-module search proves nothing about which one was found.
    The credential absences stay module-wide: a stored credential is wrong
    wherever it appears.
    """
    text = _strip_line_comments(_module_text())
    job = _job_block()
    assert "registries" in job, "the job must declare a registries block for the image pull"
    assert re.search(r"identity:\s*\w", job), "the registry pull must reference an identity"
    lowered = text.lower()
    assert "passwordsecretref" not in lowered, "registry must not use a stored credential"
    assert "administratorlogin" not in lowered, "the job must not declare an admin login"


def test_job_invokes_codified_entrypoint() -> None:
    """The job invokes the committed, unit-tested bootstrap module rather than ad
    hoc inline SQL. Read out of the job's own body, so a mention in a sibling
    resource cannot stand in for the job's actual command.
    """
    assert _ENTRYPOINT in _job_block(), (
        f"the job command must invoke the codified entrypoint {_ENTRYPOINT!r}"
    )


def test_job_passes_required_env() -> None:
    """The job injects the coordinates the bootstrap entrypoint resolves from the
    environment — the admin identity selector, the server, and the role names.

    Read out of the job's own body: an env name mentioned in a sibling resource
    is not injected into the job.
    """
    job = _job_block()
    for name in _REQUIRED_ENV:
        assert f"'{name}'" in job, f"the job must inject the {name!r} environment variable"


def test_manual_trigger() -> None:
    """The job is manually triggered — declared by the deploy, started on bring-up
    (the operator doc records the trigger; CI orchestration is a later concern).
    """
    assert re.search(r"triggerType:\s*'Manual'", _job_block()), (
        "the job must declare triggerType: 'Manual'"
    )


def test_grants_acrpull_to_bootstrap_identity() -> None:
    """The bootstrap identity is granted AcrPull on the registry so the job can
    pull the SAGE image.

    The grant's own body carries the role and the principal. Read module-wide,
    the principal check is satisfied by the ``param bootstrapIdentityPrincipalId``
    declaration alone — the parameter could be declared and never bound.
    """
    text = _strip_line_comments(_module_text())
    assert _declares_resource_type(text, _ROLE_ASSIGNMENT_TYPE), (
        f"the module must declare a {_ROLE_ASSIGNMENT_TYPE} (AcrPull) resource"
    )
    grant = _resource_block(text, "bootstrapAcrPull")
    assert grant, "postgres-bootstrap.bicep must declare the bootstrapAcrPull resource"
    assert _ACR_PULL_ROLE in text, "the role assignment must reference the AcrPull role id"
    # The binding, not bare containment: the grant's deterministic name is
    # `guid(acr.id, bootstrapIdentityPrincipalId, ...)`, so the principal appears
    # in this block whether or not it is the principal actually granted.
    assert re.search(r"principalId:\s*bootstrapIdentityPrincipalId\b", grant), (
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

    Every assertion reads this module's own call body. Over the whole file the
    scope claim is satisfied by any of the nine modules, and the ACA environment
    and Postgres FQDN are each handed to two other modules besides this one.
    """
    block = _main_module_block("postgres-bootstrap.bicep")
    assert re.search(r"scope:\s*rg\b", block), "the bootstrap module must be scoped to rg"
    assert "foundation.outputs.acaEnvironmentId" in block, (
        "the bootstrap module must consume the ACA environment id"
    )
    assert "identity.outputs.bootstrapIdentityId" in block, (
        "the bootstrap module must consume the bootstrap identity"
    )
    assert "postgres.outputs.postgresServerFqdn" in block, (
        "the bootstrap module must consume the Postgres server FQDN"
    )


def test_bootstrap_and_app_share_one_database_name() -> None:
    """The bootstrap job and the SAGE app resolve the *same* Postgres database name,
    so the database the admin pre-creates the extensions in is provably the database
    the workload connects to.

    The job injects ``PG_DATABASE`` from the module's ``postgresDatabaseName``
    parameter, and the orchestrator feeds both the bootstrap module and the
    container-apps module that name from the single
    ``postgres.outputs.postgresDatabaseName`` source. A divergence here is the
    per-database scoping mismatch that leaves the workload's ``CREATE EXTENSION``
    facing an absent extension (InsufficientPrivilege at vault load). Extensions are
    per-database in Postgres, so this agreement is load-bearing.
    """
    module = _strip_line_comments(_module_text())
    assert re.search(r"name:\s*'PG_DATABASE'\s+value:\s*postgresDatabaseName", module), (
        "the bootstrap job must inject PG_DATABASE from the postgresDatabaseName parameter"
    )
    wiring = "postgresDatabaseName: postgres.outputs.postgresDatabaseName"
    assert wiring in _main_module_block("postgres-bootstrap.bicep"), (
        "the bootstrap job's database name must come from postgres.outputs.postgresDatabaseName"
    )
    assert wiring in _main_module_block("container-apps.bicep"), (
        "the SAGE app's database name must come from the same postgres.outputs.postgresDatabaseName"
    )


def test_main_wires_bootstrap_identity_as_postgres_admin() -> None:
    """The orchestrator sets the bootstrap identity as the Postgres Entra admin so
    the job's token can administer the database — the codified admin, no deploy-time
    GUID required.

    ``aadAdminObjectId`` is the relational-store module's parameter, so the
    binding is read out of *that* module's call body rather than anywhere in the
    orchestrator.
    """
    block = _main_module_block("postgres.bicep")
    # The binding may be a multi-line ternary (operator-supplied override vs the
    # default bootstrap identity), so match across newlines but bound the span so
    # it stays within the aadAdminObjectId assignment.
    assert re.search(
        r"aadAdminObjectId:[\s\S]{0,160}?identity\.outputs\.bootstrapIdentityPrincipalId", block
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


def test_module_block_detector_controls() -> None:
    """``_module_block`` returns only the named module's own call body.

    This is what makes the wiring gates load-bearing: every module in the
    orchestrator carries ``scope: rg``, and the ACA environment and Postgres FQDN
    are each handed to three modules, so a whole-file search is satisfied by a
    neighbour even after this call has dropped them.

    The neighbouring module is declared *after* the target: a helper that finds
    the target but never truncates would still leak it, and only this ordering
    catches that. (A contaminant placed before the target is excluded by the
    forward search alone and proves nothing.)
    """
    two_modules = (
        "module postgresBootstrap 'modules/postgres-bootstrap.bicep' = {\n"
        "  params: {\n    bootstrapIdentityId: identity.outputs.bootstrapIdentityId\n  }\n}\n"
        "module maintenanceJob 'modules/maintenance-job.bicep' = {\n"
        "  scope: rg\n"
        "  params: {\n    acaEnvironmentId: foundation.outputs.acaEnvironmentId\n  }\n}\n"
    )
    block = _module_block(two_modules, "postgres-bootstrap.bicep")
    assert block, "the detector must find the postgres-bootstrap module call"
    assert "bootstrapIdentityId" in block, "the block must carry the call's own parameters"
    assert "scope: rg" not in block, (
        "the block must truncate at the next declaration, not borrow the following "
        "module's scope line"
    )
    assert "acaEnvironmentId" not in block, (
        "the block must truncate at the next declaration, not borrow the maintenance "
        "job's identical ACA environment threading"
    )
    assert _module_block(two_modules, "absent.bicep") == ""


def test_module_block_truncates_at_a_top_level_output() -> None:
    """The last module wired is followed by the orchestrator's own outputs, not by
    another module — so the slice must stop at a top-level ``output`` too, or that
    module's block runs to end of file and picks the outputs up.
    """
    last_module = (
        "module maintenanceJob 'modules/maintenance-job.bicep' = {\n"
        "  params: {\n    abstractionModel: abstractionModel\n  }\n}\n"
        "output deployedResourceGroupName string = rg.name\n"
    )
    block = _module_block(last_module, "maintenance-job.bicep")
    assert "abstractionModel" in block, "the block must carry the call's own parameters"
    assert "deployedResourceGroupName" not in block, (
        "the block must truncate at the orchestrator's top-level output"
    )


def test_resource_block_detector_controls() -> None:
    """``_resource_block`` returns only the named resource's own body.

    This is what makes the job gates load-bearing. ``identity:`` appears in both
    the job's own identity block and its registry pull, and the AcrPull grant
    sits beside them, so a property found anywhere in the module says nothing
    about which resource carries it.

    The neighbouring resource is declared *after* the target: a helper that finds
    the target but never truncates would still leak it, and only this ordering
    catches that. (A contaminant placed before the target is excluded by the
    forward search alone and proves nothing.)
    """
    sample = (
        "resource bootstrapJob 'Microsoft.App/jobs@2024-03-01' = {\n"
        "  identity: {\n    type: 'UserAssigned'\n  }\n}\n"
        "resource bootstrapAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {\n"
        "  properties: {\n    principalId: bootstrapIdentityPrincipalId\n  }\n}\n"
    )
    job = _resource_block(sample, "bootstrapJob")
    assert job, "the detector must find the bootstrapJob declaration"
    assert "UserAssigned" in job, "the block must carry the job's own identity binding"
    assert "bootstrapIdentityPrincipalId" not in job, (
        "the block must truncate at the next declaration, not leak the AcrPull grant"
    )
    grant = _resource_block(sample, "bootstrapAcrPull")
    assert "principalId" in grant, "the grant block must carry its own properties"
    assert _resource_block(sample, "absentSymbol") == ""
