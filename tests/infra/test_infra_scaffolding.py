"""Structural and security-posture gate for the ``infra/`` IaC scaffold.

Locks the shape of the Bicep deployment scaffold and the GitHub Actions
deploy workflow so later hosting-environment modules build into a stable,
secure harness. The CI deploy identity is OIDC-federated (no stored
secret); these checks fail the build if that posture regresses. The
deployment-profile model this scaffold realizes is recorded in CAS-ADR-042.

These checks read tracked files only — they need no Azure or Bicep tooling,
so they run in the ordinary Python test job. Actual Bicep compilation is
validated authoritatively by the infra workflow's ``validate`` job; a
local fast-path check is provided here, skipped when the CLI is absent.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest
import yaml

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
INFRA_DIR: Final[Path] = REPO_ROOT / "infra"
MAIN_BICEP: Final[Path] = INFRA_DIR / "main.bicep"
MAIN_BICEPPARAM: Final[Path] = INFRA_DIR / "main.bicepparam"
MAIN_BICEPPARAM_EXAMPLE: Final[Path] = INFRA_DIR / "main.bicepparam.example"
MODULES_DIR: Final[Path] = INFRA_DIR / "modules"
WORKFLOW: Final[Path] = REPO_ROOT / ".github" / "workflows" / "infra.yml"
RUNBOOK: Final[Path] = REPO_ROOT / "docs" / "process" / "azure-deployment.md"

# A subscription / tenant / client id is a GUID. None of these identity
# coordinates may be hardcoded into a tracked infra surface — they arrive
# as deployment parameters or GitHub Actions repository variables.
_GUID_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b"
)

_SURFACES: Final[tuple[Path, ...]] = (
    MAIN_BICEP,
    MAIN_BICEPPARAM,
    MAIN_BICEPPARAM_EXAMPLE,
    WORKFLOW,
    RUNBOOK,
)


def _on_block(workflow: dict) -> dict:
    """Return the workflow trigger mapping.

    PyYAML parses the bare ``on:`` key as the boolean ``True`` under YAML
    1.1 truthy-token rules, so the trigger block is keyed by ``True`` rather
    than the string ``"on"``.
    """
    block = workflow.get(True)
    if block is None:
        block = workflow.get("on")
    return block or {}


def _job_environment(job: dict) -> str | None:
    """The environment a job binds, whether given as a string or a mapping."""
    env = job.get("environment")
    if isinstance(env, str):
        return env
    if isinstance(env, dict):
        return env.get("name")
    return None


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


def test_infra_tree_exists() -> None:
    """The scaffold contract every later hosting-environment module assumes."""
    assert MAIN_BICEP.is_file(), "infra/main.bicep missing"
    assert MAIN_BICEPPARAM.is_file(), "infra/main.bicepparam missing"
    assert MODULES_DIR.is_dir(), "infra/modules/ missing"
    assert (MODULES_DIR / "README.md").is_file(), "infra/modules/README.md missing"
    assert WORKFLOW.is_file(), ".github/workflows/infra.yml missing"
    assert RUNBOOK.is_file(), "docs/process/azure-deployment.md missing"


def test_main_bicep_is_subscription_scoped_with_rg() -> None:
    """The orchestrator deploys a minimal real footprint: it targets the
    subscription and creates the resource group every module deploys into.
    """
    text = MAIN_BICEP.read_text(encoding="utf-8")
    assert re.search(r"targetScope\s*=\s*'subscription'", text), (
        "main.bicep must target the subscription scope"
    )
    assert re.search(r"resource\s+\w+\s+'Microsoft\.Resources/resourceGroups@", text), (
        "main.bicep must declare a resourceGroup resource (the minimal footprint)"
    )


def test_bicepparam_wires_to_main() -> None:
    """The parameter file binds to the orchestrator and sets the env params."""
    text = MAIN_BICEPPARAM.read_text(encoding="utf-8")
    assert re.search(r"using\s+'\./main\.bicep'", text), (
        "main.bicepparam must declare `using './main.bicep'`"
    )
    assert "environmentName" in text, "main.bicepparam must set environmentName"
    assert "location" in text, "main.bicepparam must set location"


def test_main_bicep_exports_orchestration_outputs() -> None:
    """The orchestrator exposes the bootstrap-job and container-app names the CI
    deploy pipeline resolves to start the in-VNet Postgres bootstrap job and
    converge the app tier — read from the deployment contract, not reconstructed
    from a hardcoded resource-naming convention in the workflow.
    """
    text = MAIN_BICEP.read_text(encoding="utf-8")
    for output_name in ("bootstrapJobName", "sageContainerAppName", "bffContainerAppName"):
        assert re.search(rf"output\s+{output_name}\s+string\s*=", text), (
            f"main.bicep must export {output_name} for the CI deploy orchestration"
        )


def test_infra_workflow_oidc_posture() -> None:
    """The deploy workflow uses GitHub OIDC (no stored secret), gates the PR
    run on ``infra/**`` changes, binds the dispatch-selected tenant Environment
    as its approval gate, and reads its deploy identity from a GitHub variable.
    """
    raw = WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.safe_load(raw)

    permissions = workflow.get("permissions") or {}
    assert permissions.get("id-token") == "write", "workflow must request id-token: write for OIDC"

    lowered = raw.lower()
    for forbidden in ("client-secret", "client_secret", "azure_client_secret", "creds:"):
        assert forbidden not in lowered, (
            f"deploy identity must be OIDC-federated, not a stored secret ({forbidden!r})"
        )

    pull_request = _on_block(workflow).get("pull_request") or {}
    paths = pull_request.get("paths") or []
    assert any("infra/" in str(p) for p in paths), (
        "pull_request trigger must be path-filtered to infra/**"
    )

    # The deploy is per-tenant: an operator triggers it and selects the tenant's
    # Environment by a workflow_dispatch input, and the apply job binds that
    # selected environment as the approval gate — never a hardcoded tenant.
    dispatch = _on_block(workflow).get("workflow_dispatch") or {}
    assert "environment" in (dispatch.get("inputs") or {}), (
        "workflow_dispatch must declare the tenant `environment` input"
    )
    jobs = workflow.get("jobs") or {}
    bound = [e for e in (_job_environment(job) for job in jobs.values()) if e]
    assert bound, "a job must bind a GitHub Environment (the approval gate)"
    assert any("inputs.environment" in e for e in bound), (
        "the apply job must bind the dispatch-selected tenant environment, not a literal"
    )

    assert "vars.AZURE_CLIENT_ID" in raw, (
        "Azure-touching jobs must be gated on the AZURE_CLIENT_ID variable"
    )


def test_infra_surfaces_have_no_hardcoded_identity() -> None:
    """No subscription/tenant/client GUID or repository owner is baked into a
    tracked infra surface — identity arrives via parameters and variables.
    """
    owner = _git_owner()
    for surface in _SURFACES:
        if not surface.is_file():
            continue
        text = surface.read_text(encoding="utf-8", errors="replace")
        assert not _GUID_RE.search(text), (
            f"{surface.name}: hardcoded GUID; use a parameter or repository variable"
        )
        if owner:
            assert owner.lower() not in text.lower(), (
                f"{surface.name}: hardcoded repository owner; use the GitHub repository context"
            )


def test_federated_subjects_are_templated() -> None:
    """Every federated-credential subject in the runbook is templated — a
    variable expansion (``${...}``) or an ``<OWNER>/<REPO>`` placeholder —
    never a literal owner/repo pair.
    """
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "github.repository" in text or "<OWNER>/<REPO>" in text, (
        "runbook must show the dynamic repo slug for federated credentials"
    )
    for match in re.finditer(r"repo:(\S+)", text):
        subject = match.group(1)
        assert subject.startswith("${") or subject.startswith("<"), (
            f"federated-credential subject must be templated, not literal: {subject!r}"
        )


@pytest.mark.skipif(
    shutil.which("bicep") is None and shutil.which("az") is None,
    reason="bicep/az CLI absent; the infra workflow validate job is authoritative",
)
def test_main_bicep_compiles(tmp_path: Path) -> None:
    """The orchestrator compiles to ARM JSON with no error (local fast check)."""
    outfile = tmp_path / "main.json"
    if shutil.which("bicep") is not None:
        cmd = ["bicep", "build", str(MAIN_BICEP), "--outfile", str(outfile)]
    else:
        cmd = ["az", "bicep", "build", "--file", str(MAIN_BICEP), "--outfile", str(outfile)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"bicep build failed:\n{proc.stderr}"
