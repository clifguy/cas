"""Structural gate for the per-tenant CI deploy orchestration.

Locks the shape of ``.github/workflows/infra.yml`` as the authoritative,
dispatch-driven cloud deploy path parameterized per tenant via GitHub
Environments (CAS-ADR-042; the CAS Cloud Deployment Discipline). An operator
triggers the deploy and selects the target tenant's Environment, which carries
that tenant's federated deploy identity and its parameter set. The deploy job
orchestrates the full staged bring-up — build/push, a what-if gate, the Bicep
apply, the in-VNet Postgres bootstrap job, app-tier convergence, and the
post-deploy preflight — so multi-pass convergence is handled by CI rather than
by an operator re-running deploys.

These checks read the tracked workflow YAML only; the authoritative Bicep
compile is the workflow's own validate job. Command-present assertions anchor on
the command line that does the work, never on prose, so a paraphrasing comment
cannot satisfy a stage gate.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import yaml

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
WORKFLOW: Final[Path] = REPO_ROOT / ".github" / "workflows" / "infra.yml"

# A subscription / tenant / client id is a GUID. None of these identity
# coordinates may be hardcoded into the deploy workflow — they arrive as
# environment-scoped GitHub variables at deploy time.
_GUID_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b"
)


def _load() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _on_block(workflow: dict) -> dict:
    """The workflow trigger mapping (PyYAML keys bare ``on:`` as ``True``)."""
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


def _job_run_text(job: dict) -> str:
    """All ``run:`` script of a job, joined in step order."""
    steps = job.get("steps") or []
    return "\n".join(s.get("run", "") for s in steps if isinstance(s, dict))


def _deploy_job(workflow: dict) -> dict:
    """The job that applies the deployment — it runs ``az deployment sub create``."""
    for job in (workflow.get("jobs") or {}).values():
        if "az deployment sub create" in _job_run_text(job):
            return job
    raise AssertionError("no deploy job runs `az deployment sub create`")


def test_dispatch_selects_tenant_environment() -> None:
    """The deploy is operator-triggered and selects the tenant's Environment by a
    workflow_dispatch input; the deploy job binds that selected environment rather
    than a hardcoded tenant.
    """
    workflow = _load()
    dispatch = _on_block(workflow).get("workflow_dispatch") or {}
    inputs = dispatch.get("inputs") or {}
    assert "environment" in inputs, (
        "workflow_dispatch must take an `environment` input selecting the tenant"
    )
    env = _job_environment(_deploy_job(workflow))
    assert env is not None, "the deploy job must bind a GitHub Environment (the approval gate)"
    assert "inputs.environment" in env, (
        f"the deploy environment must be the dispatch-selected tenant, not a literal: {env!r}"
    )


def test_deploy_orchestrates_staged_bringup_in_order() -> None:
    """The deploy job runs the full staged bring-up, and the stages appear in the
    order convergence requires: what-if gate, Bicep apply, the in-VNet bootstrap
    job, app-tier convergence, then the post-deploy preflight.
    """
    runs = _job_run_text(_deploy_job(_load()))
    anchors = [
        "az deployment sub what-if",
        "az deployment sub create",
        "az containerapp job start",
        "az containerapp revision restart",
        "deploy/cloud-preflight.sh",
    ]
    positions: list[int] = []
    for anchor in anchors:
        idx = runs.find(anchor)
        assert idx != -1, f"deploy job missing orchestration stage: {anchor!r}"
        positions.append(idx)
    assert positions == sorted(positions), (
        f"deploy stages out of order: {list(zip(anchors, positions))}"
    )


def test_whatif_gate_precedes_apply() -> None:
    """A what-if / validate gate runs before the apply and so can block on a Bicep
    error before any change is made.
    """
    runs = _job_run_text(_deploy_job(_load()))
    whatif = runs.find("az deployment sub what-if")
    apply = runs.find("az deployment sub create")
    assert whatif != -1, "deploy job must run a what-if gate"
    assert apply != -1, "deploy job must apply the deployment"
    assert whatif < apply, "the what-if gate must run before the apply"


def test_build_uses_reusable_workflow_and_deploy_needs_it() -> None:
    """Images come from the shared reusable build workflow (single source of the
    build), invoked with push enabled for the tenant's registry; the deploy job
    depends on that build.
    """
    workflow = _load()
    jobs = workflow.get("jobs") or {}
    build_jobs = [
        name
        for name, job in jobs.items()
        if isinstance(job.get("uses"), str) and "build-images.yml" in job["uses"]
    ]
    assert build_jobs, "a job must call the reusable ./.github/workflows/build-images.yml"
    for name in build_jobs:
        with_block = jobs[name].get("with") or {}
        assert str(with_block.get("push")).lower() == "true", (
            "the deploy build job must call the reusable workflow with push: true"
        )
    deploy = _deploy_job(workflow)
    needs = deploy.get("needs")
    needs_list = [needs] if isinstance(needs, str) else list(needs or [])
    assert any(n in build_jobs for n in needs_list), (
        "the deploy job must depend on the reusable build job"
    )


def test_parameters_sourced_from_variables_not_repo() -> None:
    """The apply targets the orchestrator template directly and passes inline
    parameters; it consumes no committed per-tenant ``.bicepparam`` — tenant-isms
    stay out of the repository and arrive as environment variables at deploy time.
    """
    runs = _job_run_text(_deploy_job(_load()))
    assert "infra/main.bicep" in runs, "deploy must target the infra/main.bicep template"
    assert "--parameters" in runs, "deploy must pass inline parameters"
    assert not re.search(r"main\.[A-Za-z0-9_-]+\.bicepparam", runs), (
        "deploy must not consume a committed per-tenant .bicepparam; parameters come "
        "from environment variables at deploy time"
    )


def test_no_hardcoded_identity_in_workflow() -> None:
    """No subscription/tenant/client GUID is baked into the workflow; the deploy
    identity is read from a GitHub variable.
    """
    raw = WORKFLOW.read_text(encoding="utf-8")
    assert not _GUID_RE.search(raw), "no identity GUID may be hardcoded in the deploy workflow"
    assert "vars.AZURE_CLIENT_ID" in raw, (
        "Azure-touching jobs must read the deploy identity from a GitHub variable"
    )


def test_oidc_posture_preserved() -> None:
    """The workflow requests the OIDC token and carries no stored client secret."""
    workflow = _load()
    raw = WORKFLOW.read_text(encoding="utf-8")
    permissions = workflow.get("permissions") or {}
    assert permissions.get("id-token") == "write", "workflow must request id-token: write for OIDC"
    lowered = raw.lower()
    for forbidden in ("client-secret", "client_secret", "azure_client_secret", "creds:"):
        assert forbidden not in lowered, (
            f"deploy identity must be OIDC-federated, not a stored secret ({forbidden!r})"
        )
