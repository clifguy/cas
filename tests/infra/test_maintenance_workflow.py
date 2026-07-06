"""Structural gate for the out-of-band maintenance workflow.

Locks the shape of ``.github/workflows/maintenance.yml`` — the dispatch-only path
that starts the already-deployed in-VNet maintenance job (CAS-ADR-043/034/029).
The irreversible operations in SAGE (whole-vault teardown, document purge) stay off
the reachable API surface and off the routine deploy pipeline: they run only on an
explicit operator dispatch, under a tenant Environment's required-reviewer approval
gate, with the command selector and the per-invocation request threaded to the job
as env-var overrides. This workflow does not apply infrastructure — the job is
deployed by infra.yml — so it must not carry a Bicep apply.

These checks read the tracked workflow YAML only; no Azure tooling.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import yaml

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
WORKFLOW: Final[Path] = REPO_ROOT / ".github" / "workflows" / "maintenance.yml"

_REQUIRED_INPUTS: Final[tuple[str, ...]] = (
    "environment",
    "command",
    "vault_id",
    "target",
    "confirm",
    "apply",
    "snapshot",
    "reason",
)

_COMMANDS: Final[tuple[str, ...]] = (
    "delete_vault",
    "purge_document",
    "purge_chain",
    "purge_batch",
)


def _load() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _on_block(workflow: dict) -> dict:
    """The workflow trigger mapping (PyYAML keys bare ``on:`` as ``True``)."""
    block = workflow.get(True)
    if block is None:
        block = workflow.get("on")
    return block or {}


def _maintenance_job(workflow: dict) -> dict:
    return (workflow.get("jobs") or {})["maintenance"]


def _job_run_text(job: dict) -> str:
    return "\n".join(s.get("run", "") for s in (job.get("steps") or []) if isinstance(s, dict))


def _invocation(run: str, marker: str) -> str:
    """The single shell invocation starting at ``marker``: its first line plus
    every backslash-continued line, so flag assertions scope to one command
    rather than the whole script."""
    lines = run[run.index(marker) :].splitlines()
    taken: list[str] = []
    for line in lines:
        taken.append(line)
        if not line.rstrip().endswith("\\"):
            break
    return "\n".join(taken)


def _job_environment(job: dict) -> str | None:
    env = job.get("environment")
    if isinstance(env, str):
        return env
    if isinstance(env, dict):
        return env.get("name")
    return None


# ---------------------------------------------------------------------------
# Structural gates
# ---------------------------------------------------------------------------


def test_workflow_exists() -> None:
    assert WORKFLOW.is_file(), ".github/workflows/maintenance.yml missing"


def test_dispatch_inputs_present() -> None:
    """The workflow is dispatch-only and takes the per-invocation maintenance
    request, including the command selector covering every maintenance mode."""
    on = _on_block(_load())
    assert "workflow_dispatch" in on, "maintenance must be workflow_dispatch (operator-triggered)"
    assert set(on) <= {"workflow_dispatch"}, (
        "maintenance must not run on push/pull_request — it is out-of-band only"
    )
    inputs = (on.get("workflow_dispatch") or {}).get("inputs") or {}
    for name in _REQUIRED_INPUTS:
        assert name in inputs, f"the dispatch must take a {name!r} input"
    assert tuple((inputs["command"] or {}).get("options") or ()) == _COMMANDS, (
        "the command selector must cover exactly the dispatcher's command set"
    )


def test_bound_to_environment_approval_gate() -> None:
    """The maintenance job binds the dispatch-selected tenant Environment, whose
    required reviewer is the approval gate."""
    job = _maintenance_job(_load())
    assert _job_environment(job) == "${{ inputs.environment }}", (
        "the maintenance job must bind environment: ${{ inputs.environment }} (approval gate)"
    )


def test_oidc_login_no_stored_secret() -> None:
    """OIDC federated login (id-token: write + azure/login), never a stored secret."""
    workflow = _load()
    perms = workflow.get("permissions") or {}
    assert perms.get("id-token") == "write", "the workflow must request an OIDC id-token"
    uses = [s.get("uses", "") for s in (_maintenance_job(workflow).get("steps") or [])]
    assert any(u.startswith("azure/login") for u in uses), "the job must log in via azure/login"


def test_request_applied_via_update_then_plain_start() -> None:
    """The job resolves the deployed maintenance job, applies the command
    selector and request to the job's environment with ``az containerapp job
    update --set-env-vars`` (a merge, so the baked coordinates survive), starts
    the job plain so the execution inherits the full deployed template, and
    polls to a terminal status.

    Anti-coincidental-pass: ``--set-env-vars`` contains ``--env-vars`` as a
    substring, so the no-override check scopes to the extracted start
    invocation via ``_invocation``. A start carrying any container-override
    flag (``--env-vars``, ``--image``, ``--command``) makes the CLI rebuild the
    execution's container spec from the flags alone — dropping the deployed
    image and command — so that shape must fail here, as must an update placed
    after the start (the request would miss the execution).
    """
    run = _job_run_text(_maintenance_job(_load()))
    assert "maintenanceJobName" in run, "the job name must resolve from the deployment output"
    assert "az containerapp job update" in run, "the request must be applied to the job env"
    update = _invocation(run, "az containerapp job update")
    assert '--set-env-vars "${ENV_VARS[@]}"' in update, (
        "the update must merge the request env (not replace it)"
    )
    assert "az containerapp job start" in run, "the job must be started"
    assert run.index("az containerapp job update") < run.index("az containerapp job start"), (
        "the env must be applied before the start"
    )
    start = _invocation(run, "az containerapp job start")
    for flag in ("--env-vars", "--image", "--command"):
        assert flag not in start, (
            f"the start must not carry {flag} — any container-override flag makes the"
            " execution's container spec rebuild from the flags, dropping the template"
        )
    assert "SAGE_MAINTENANCE_COMMAND=$COMMAND" in run, "the update must thread the command selector"
    for var in (
        "SAGE_DELETE_VAULT_ID",
        "SAGE_DELETE_CONFIRM",
        "SAGE_DELETE_APPLY",
        "SAGE_PURGE_VAULT_ID",
        "SAGE_PURGE_CONFIRM",
        "SAGE_PURGE_APPLY",
    ):
        assert var in run, f"the update must thread {var}"
    assert "az containerapp job execution show" in run, "the job must poll for a terminal status"


def test_failure_log_fetch_names_the_container() -> None:
    """The failure-path log fetch passes ``--container maintenance`` — the
    container name the maintenance-job module declares.

    Anti-coincidental-pass: the assertion scopes to the ``az containerapp job
    logs show`` invocation via ``_invocation``, so a comment elsewhere naming
    the container cannot satisfy it. Without the flag the fetch errors instead
    of resolving logs, hiding the diagnostics of a failed destructive run.
    """
    run = _job_run_text(_maintenance_job(_load()))
    assert "az containerapp job logs show" in run, "a failed run must fetch recent logs"
    logs = _invocation(run, "az containerapp job logs show")
    assert "--container maintenance" in logs, (
        "the log fetch must name the deployed container explicitly"
    )


def test_does_not_apply_infrastructure() -> None:
    """The maintenance workflow starts an already-deployed job; it must not run a
    Bicep apply (that stays in infra.yml). A stray apply here would couple the
    destructive path to the deploy pipeline.
    """
    run = _job_run_text(_maintenance_job(_load()))
    assert "az deployment sub create" not in run, "maintenance must not apply infrastructure"
    assert "what-if" not in run, "maintenance must not run a deploy what-if"


def test_request_threaded_through_env_not_interpolated() -> None:
    """The dispatch inputs reach the script through env vars, not direct
    ``${{ inputs.* }}`` interpolation — the injection-safe pattern.

    Anti-coincidental-pass: assert the env block maps each request var from its input
    *and* the run script references the shell vars (``$VAULT_ID``, ``$TARGET``), not
    ``${{ inputs.* }}``. A script that interpolated the input directly would let a
    crafted vault id or target inject shell.
    """
    job = _maintenance_job(_load())
    env = job.get("env") or {}
    assert env.get("COMMAND") == "${{ inputs.command }}"
    assert env.get("VAULT_ID") == "${{ inputs.vault_id }}"
    assert env.get("TARGET") == "${{ inputs.target }}"
    assert env.get("CONFIRM") == "${{ inputs.confirm }}"
    assert env.get("REASON") == "${{ inputs.reason }}"
    run = _job_run_text(job)
    assert "SAGE_DELETE_VAULT_ID=$VAULT_ID" in run, (
        "the start must use the env var, not the raw input"
    )
    assert "SAGE_PURGE_DOCUMENT_ID=$TARGET" in run, (
        "the start must use the env var, not the raw input"
    )
    assert "${{ inputs." not in run, "inputs must not be interpolated into the run script"
