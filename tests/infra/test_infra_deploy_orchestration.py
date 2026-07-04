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
MAIN_BICEP: Final[Path] = REPO_ROOT / "infra" / "main.bicep"

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


def _uncommented_run_text(job: dict) -> str:
    """``_job_run_text`` with shell ``#`` comment lines removed, so a command named
    only in a comment cannot be mistaken for a real invocation by a token-mint gate.
    """
    return "\n".join(
        line for line in _job_run_text(job).splitlines() if not line.lstrip().startswith("#")
    )


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


def _bootstrap_job_wait_step_run(workflow: dict) -> str:
    """The ``run:`` script of the deploy step that starts the in-VNet bootstrap job."""
    for job in (workflow.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            run = step.get("run", "") if isinstance(step, dict) else ""
            if "az containerapp job start" in run:
                return run
    raise AssertionError("no step runs `az containerapp job start`")


def test_bootstrap_job_wait_fails_on_non_terminal_timeout() -> None:
    """The bootstrap-job wait must fail the deploy if the job never reaches a
    terminal status within the wait budget — it must not fall through to success.

    The poll loop breaks on ``Succeeded`` and exits non-zero on a terminal failure,
    but a bounded loop that simply *ends* (the job still Running/Pending at the last
    iteration) must not let the deploy proceed to restart the app tier against an
    incomplete bootstrap — the path that leaves the workload's ``CREATE EXTENSION``
    facing an as-yet-uncreated extension (InsufficientPrivilege at vault load). A
    sentinel set only on ``Succeeded`` plus a post-loop guard that exits non-zero
    closes that fall-through. Anchored on the control lines, not prose.
    """
    run = _bootstrap_job_wait_step_run(_load())
    assert "az containerapp job execution show" in run, (
        "the wait must poll the job execution status"
    )
    # A terminal-failure status fails the step from inside the loop.
    assert re.search(r"Failed\|Degraded\|Stopped\)[^\n]*exit 1", run), (
        "a terminal-failure status must fail the step"
    )
    # Success is recorded only on the Succeeded arm...
    assert re.search(r"Succeeded\)\s+BOOTSTRAP_SUCCEEDED=true", run), (
        "the wait must record success only on the Succeeded status"
    )
    # ...and after the loop a non-success sentinel fails the step, so a timed-out
    # (still non-terminal) job cannot pass silently.
    collapsed = re.sub(r"[ \t]+", " ", run)
    guard = re.search(r'if \[ "\$BOOTSTRAP_SUCCEEDED" != true \]', collapsed)
    assert guard is not None, "a post-loop guard must check the success sentinel"
    assert "exit 1" in collapsed[guard.start() :], (
        "the post-loop guard must exit non-zero when the job never reached Succeeded"
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


def _required_bicep_param_names() -> list[str]:
    """Every ``main.bicep`` parameter declared with no default value.

    A required parameter with no matching entry in the deploy job's
    ``--parameters`` list fails ``az deployment sub create`` outright — the
    live-deploy-only failure mode this gate exists to catch before a dispatch.
    """
    names: list[str] = []
    for line in MAIN_BICEP.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*param\s+(\w+)\b(.*)$", line)
        if match and "=" not in match.group(2):
            names.append(match.group(1))
    return names


def test_deploy_passes_every_required_bicep_parameter() -> None:
    """Every required (no-default) ``main.bicep`` parameter is passed by name in
    both the what-if gate and the apply step's ``--parameters`` list.

    A required parameter added to the template without a matching
    ``name="$VAR"`` entry here compiles clean, passes the workflow's own
    structural gates, and only fails on a live ``az deployment sub create`` —
    exactly the class of change this repo's azure-deploy-review discipline
    flags as non-locally-verifiable.
    """
    required = _required_bicep_param_names()
    assert required, "no required params parsed from main.bicep (parser drift?)"
    runs = _job_run_text(_deploy_job(_load()))
    for name in required:
        assert re.search(rf"\b{re.escape(name)}=", runs), (
            f"deploy job must pass required main.bicep parameter {name!r} "
            "(missing from --parameters would fail az deployment sub create)"
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


def test_deploy_gate_not_keyed_on_environment_scoped_var() -> None:
    """The deploy job's job-level ``if`` must not gate on an environment-scoped
    variable. GitHub does not expose environment variables to a job-level ``if``
    (only repository/org vars), so gating the apply on ``vars.AZURE_CLIENT_ID`` —
    which is environment-scoped in the per-tenant model — evaluates empty and
    skips the deploy forever. The apply is gated by the dispatch event plus the
    environment binding and the ``azure/login`` step instead.
    """
    deploy = _deploy_job(_load())
    condition = str(deploy.get("if") or "")
    assert "vars." not in condition, (
        "deploy job `if` must not reference an environment-scoped var (invisible at "
        f"job-level if); gate via the dispatch event + environment binding. Got: {condition!r}"
    )


def test_preflight_mints_v2_scoped_token() -> None:
    """The post-deploy preflight mints its bearer via the v2 *scope* endpoint
    (``--scope <audience>/.default``), never the v1 ``--resource`` endpoint.

    ``--resource`` always returns a token whose issuer is ``sts.windows.net``
    (``ver=1.0``); the APIM ``validate-jwt`` policy and the SAGE backend both
    expect a ``…/v2.0`` issuer, so a v1 token is rejected with 401 at the edge
    before the request ever reaches the backend. ``--resource`` also ignores the
    resource app's ``requestedAccessTokenVersion``, so only moving the mint to the
    ``/.default`` scope endpoint actually yields a v2 token.
    """
    runs = _uncommented_run_text(_deploy_job(_load()))
    # Strip shell comments (above), then collapse line-continuations so the flag and
    # its audience read as one logical command line. Comment-stripping is load-bearing:
    # the mint anchor `az account get-access-token` can appear in an adjacent v1/v2
    # explainer comment, and re.search would otherwise match that comment first and go
    # blind to a --resource (v1) regression in the real command below it.
    collapsed = re.sub(r"\s+", " ", runs.replace("\\\n", " "))
    match = re.search(r"az account get-access-token.*?\)", collapsed)
    assert match is not None, "preflight must mint a bearer token for the SAGE audience"
    mint = match.group(0)
    assert '--scope "$SAGE_AUDIENCE/.default"' in mint, (
        "preflight must mint via the v2 scope endpoint (--scope <audience>/.default), "
        f"whose issuer matches APIM and the SAGE backend; got: {mint!r}"
    )
    assert "--resource" not in mint, (
        "preflight must not mint via --resource (the v1 token endpoint; it ignores the "
        "resource app's requestedAccessTokenVersion and always returns iss=sts.windows.net)"
    )


def test_uncommented_run_text_strips_comment_lines() -> None:
    """A command named only in a shell comment is absent from the scanned text, so a
    comment cannot be mistaken for a real invocation. Without this, the re.search mint
    gate above would match a v1/v2 explainer comment first and mask a --resource
    regression in the real command below it.
    """
    job = {
        "steps": [
            {
                "run": (
                    "# mint with az account get-access-token --scope ... (v2), not --resource\n"
                    'AUTH_TOKEN="$(az account get-access-token '
                    '--scope "$SAGE_AUDIENCE/.default" -o tsv)"\n'
                )
            }
        ]
    }
    text = _uncommented_run_text(job)
    assert "# mint with" not in text, "shell comment lines must be stripped"
    assert text.count("az account get-access-token") == 1, (
        "only the real invocation survives; the comment's phantom mention is gone"
    )
    assert "not --resource" not in text, "the comment's --resource mention must not survive"
