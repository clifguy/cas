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

import re
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
    "reabstract",
)

MODULE: Final[Path] = REPO_ROOT / "infra" / "modules" / "maintenance-job.bicep"


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


def _case_arm(run: str, label: str) -> str:
    """The body of the ``case`` arm labelled ``label``: from ``label)`` to the
    arm's closing ``;;``, so a per-command assertion cannot be satisfied (or
    violated) by another arm's text."""
    start = run.index(f"{label})")
    return run[start : run.index(";;", start)]


def _all_invocations(run: str, marker: str) -> list[str]:
    """Every shell invocation starting at an occurrence of ``marker`` — each its
    first line plus backslash-continued lines — for commands the script issues
    more than once."""
    found: list[str] = []
    cursor = 0
    while (at := run.find(marker, cursor)) != -1:
        found.append(_invocation(run[at:], marker))
        cursor = at + len(marker)
    return found


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
        "SAGE_REABSTRACT_VAULT_ID",
        "SAGE_REABSTRACT_STATUSES",
        "SAGE_REABSTRACT_LIMIT",
        "SAGE_REABSTRACT_CONFIRM",
        "SAGE_REABSTRACT_APPLY",
        "SAGE_REABSTRACT_REASON",
    ):
        assert var in run, f"the update must thread {var}"
    assert "az containerapp job execution show" in run, "the job must poll for a terminal status"


def test_retry_limit_initialized_to_zero_before_the_command_case() -> None:
    """Every dispatch resolves an explicit replica-retry limit, defaulted to zero
    before the command dispatch, so an arm that never touches it — every
    destructive arm — runs with no auto-retry (CAS-ADR-029).

    Anti-coincidental-pass: the initialization must precede the ``case`` — a
    default established later (or not at all) would let a destructive dispatch
    reach the update with the variable unset (the script runs under ``set -u``)
    or carrying a leftover value.
    """
    run = _job_run_text(_maintenance_job(_load()))
    assert "RETRY_LIMIT=0" in run, "the run script must default RETRY_LIMIT to 0"
    assert run.index("RETRY_LIMIT=0") < run.index('case "$COMMAND"'), (
        "the zero default must be established before the command dispatch"
    )


def test_only_the_reabstract_arm_raises_the_retry_limit() -> None:
    """Retry tolerance is scoped to the non-destructive sweep: only the
    ``reabstract`` arm may raise the replica-retry limit. Re-execution of a
    destructive command stays a deliberate operator dispatch, never a silent
    platform retry (CAS-ADR-029).

    Anti-coincidental-pass: exactly two assignments are allowed — the zero
    default and the reabstract raise, located inside that arm via ``_case_arm``
    — and each destructive arm is asserted not to touch the variable at all, so
    neither a stray third assignment nor a raise that migrated into a
    destructive arm can pass.
    """
    run = _job_run_text(_maintenance_job(_load()))
    assignments = re.findall(r"RETRY_LIMIT=(\d+)", run)
    assert assignments, "the run script must assign RETRY_LIMIT"
    assert assignments[0] == "0", "the first assignment must be the zero default"
    assert len(assignments) == 2, (
        f"exactly the zero default and the single reabstract raise are allowed; found {assignments}"
    )
    assert int(assignments[1]) >= 1, "the reabstract arm must raise the limit"
    assert f"RETRY_LIMIT={assignments[1]}" in _case_arm(run, "reabstract"), (
        "the raise must live inside the reabstract arm"
    )
    for arm in ("delete_vault", "purge_document", "purge_chain", "purge_batch"):
        assert "RETRY_LIMIT" not in _case_arm(run, arm), (
            f"the {arm} arm must not touch the retry limit"
        )


def test_update_always_pins_the_retry_limit() -> None:
    """The job update carries ``--replica-retry-limit "$RETRY_LIMIT"`` on every
    dispatch. ``az containerapp job update`` mutates the standing job spec, so a
    limit raised for one dispatch persists until something overwrites it;
    re-pinning it on the same invocation that applies the request env is what
    keeps a destructive dispatch at zero after a reabstract raised it
    (CAS-ADR-029).

    Anti-coincidental-pass: the assertion scopes to the update invocation via
    ``_invocation`` — the flag mentioned in a comment, or moved to a
    conditional second update that a destructive dispatch skips, cannot pass.
    The update must also be unique: a second update elsewhere could hardcode a
    raised limit past the variable discipline, and ``_invocation`` reads only
    the first occurrence.
    """
    run = _job_run_text(_maintenance_job(_load()))
    assert run.count("az containerapp job update") == 1, (
        "exactly one job update is allowed — a second could re-raise the retry limit"
    )
    update = _invocation(run, "az containerapp job update")
    assert '--replica-retry-limit "$RETRY_LIMIT"' in update, (
        "the update must pin the per-dispatch retry limit"
    )


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


def test_failure_path_dumps_the_execution_detail() -> None:
    """On a failure the workflow dumps the execution record itself.

    A pre-start failure leaves no container to stream logs from, so the log
    fetch alone returns a not-found error and nothing else; the execution
    record always exists once the start succeeded and carries the status and
    timestamps.

    Anti-coincidental-pass: the poll loop issues the same command with
    ``--query properties.status``, so the assertion requires an invocation
    carrying ``--output json`` — the loop alone cannot satisfy it.
    """
    run = _job_run_text(_maintenance_job(_load()))
    shows = _all_invocations(run, "az containerapp job execution show")
    assert any("--output json" in show for show in shows), (
        "the failure path must dump the execution record as JSON"
    )


def test_failure_path_queries_system_logs_by_job_name() -> None:
    """On a failure the workflow queries the Log Analytics system-log table —
    the only table with rows when no container ever started — filtered on the
    job-name column.

    Anti-coincidental-pass traps, each with its own assertion: Container App
    Jobs log with an empty container-app-name column, so filtering on it
    returns zero rows and reads as "no logs exist"; and the workspace id must
    compose from the deployment outputs rather than re-deriving the workspace
    naming convention in the workflow, where drift would surface only live, on
    a failure.
    """
    run = _job_run_text(_maintenance_job(_load()))
    assert "az monitor log-analytics query" in run, (
        "a failed run must query the workspace for the job's system logs"
    )
    query = _invocation(run, "az monitor log-analytics query")
    assert "ContainerAppSystemLogs_CL" in query, (
        "the query must read the system-log table (the only one populated on a pre-start failure)"
    )
    assert "JobName_s ==" in query, "the query must filter on the job-name column"
    assert "ContainerAppName_s" not in query, (
        "Container App Jobs log with an empty ContainerAppName_s — filtering on"
        " it returns zero rows"
    )
    assert "properties.outputs.logAnalyticsCustomerId.value" in run, (
        "the workspace id must resolve from the deployment outputs"
    )
    assert "log-${ENVIRONMENT_NAME}" not in run, (
        "the workflow must not re-derive the workspace naming convention"
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


# ---------------------------------------------------------------------------
# Detector controls
# ---------------------------------------------------------------------------


def test_case_arm_scopes_to_one_arm() -> None:
    """``_case_arm`` must stop at the arm's ``;;`` — otherwise a per-arm
    assertion could be satisfied (or violated) by a neighboring arm's text."""
    sample = 'case "$X" in\n  alpha)\n    FIRST=1 ;;\n  beta)\n    SECOND=2 ;;\nesac'
    arm = _case_arm(sample, "alpha")
    assert "FIRST=1" in arm
    assert "SECOND=2" not in arm


def test_all_invocations_returns_each_occurrence() -> None:
    """``_all_invocations`` must return every occurrence with its own continued
    lines — a single-occurrence extraction would let the poll loop's invocation
    stand in for the failure path's."""
    sample = "az thing show \\\n  --query a\nfiller\naz thing show \\\n  --output json\n"
    got = _all_invocations(sample, "az thing show")
    assert len(got) == 2
    assert "--query a" in got[0]
    assert "--output json" not in got[0]
    assert "--output json" in got[1]
