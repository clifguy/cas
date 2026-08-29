"""Repo-wide gate: every Container Apps Job poll loop outwaits the job it starts.

A workflow that starts a Container Apps Job and waits for it holds two numbers
that must agree but live in different files: the workflow's poll budget, and the
``replicaTimeout`` / ``replicaRetryLimit`` the Bicep module declares for the job
being polled. Nothing in the source binds them, so either can move alone. When
the poll budget falls short of ``replicaTimeout x (replicaRetryLimit + 1)``, a
job execution that *legitimately retries* is reported as a failure while the
retry is still running -- and for the cloud deploy that aborts the whole run and
abandons an in-flight execution (CAS-ADR-042, CAS-ADR-043).

A second, quieter failure comes from bounding the wait by an iteration count.
``for _ in $(seq 1 N); do ...; sleep M; done`` costs ``N x (M + az latency)``,
not ``N x M``; at a few seconds per ``az containerapp job execution show`` the
loop's nominal ceiling is unreachable inside the job's ``timeout-minutes``, the
runner kills the job mid-loop, and any post-loop failure-diagnostics tail never
runs -- in exactly the hang it was written for. So the budget must be a
wall-clock deadline, which per-iteration latency cannot erode.

This gate binds all three relations for every polling arm in the repository:
the arm's declared replica timeout equals its module's, its budget is *derived*
from the replica settings rather than hand-set, and the GitHub job timeout
outlasts the budget plus whatever the arm reserves for diagnostics. It also
discovers polling arms rather than trusting a hand-kept list, so a new one
cannot be added outside the gate's reach.

The checks read tracked workflow YAML and Bicep source only; no Azure tooling.
The controls at the bottom prove each detector fires on the regression it is
meant to catch -- a numeric gate whose extractor silently returns ``None``
would pass every arm vacuously.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
import yaml

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR: Final[Path] = REPO_ROOT / ".github" / "workflows"
MODULES_DIR: Final[Path] = REPO_ROOT / "infra" / "modules"

# The call that marks a step as polling a job execution to completion.
_POLL_MARKER: Final[str] = "az containerapp job execution show"

# The per-dispatch re-pin of the deployed retry limit. An arm that carries it may
# raise the limit above the module's declared default; an arm that does not must
# mirror that default exactly.
_RETRY_REPIN_MARKER: Final[str] = "--replica-retry-limit"

_REPLICA_TIMEOUT_RE: Final[re.Pattern[str]] = re.compile(r"replicaTimeout:\s*(\d+)")
_REPLICA_RETRY_RE: Final[re.Pattern[str]] = re.compile(r"replicaRetryLimit:\s*(\d+)")

_DECLARED_TIMEOUT_RE: Final[re.Pattern[str]] = re.compile(r"^\s*REPLICA_TIMEOUT=(\d+)", re.M)
_RETRY_LIMIT_RE: Final[re.Pattern[str]] = re.compile(r"^\s*RETRY_LIMIT=(\d+)", re.M)
_SLACK_RE: Final[re.Pattern[str]] = re.compile(r"^\s*POLL_SLACK=(\d+)", re.M)
_BUDGET_RE: Final[re.Pattern[str]] = re.compile(r"^\s*POLL_BUDGET=\$\(\((.+?)\)\)", re.M | re.S)

# A wall-clock deadline: computed once from the clock, then compared against the
# clock on every pass. Both halves are required -- a deadline that is never read
# bounds nothing, and a `date` comparison against a non-deadline bounds nothing.
_DEADLINE_ASSIGN_RE: Final[re.Pattern[str]] = re.compile(
    r"POLL_DEADLINE=\$\(\(\s*\$\(\s*date\s+\+%s\s*\)\s*\+\s*POLL_BUDGET\s*\)\)"
)
_DEADLINE_LOOP_RE: Final[re.Pattern[str]] = re.compile(
    r"while\s+\[[^]]*date\s+\+%s[^]]*POLL_DEADLINE[^]]*\]"
)
# The iteration-count bound this gate exists to forbid.
_ITERATION_LOOP_RE: Final[re.Pattern[str]] = re.compile(r"for\s+\S+\s+in\s+\$\(\s*seq\s+1\s+(\d+)")


@dataclass(frozen=True)
class PollingArm:
    """One workflow job that starts a Container Apps Job and waits for it."""

    label: str
    workflow: Path
    job: str
    module: Path
    # Wall-clock the arm must still have after the poll budget expires, for the
    # post-loop failure-diagnostics tail to run. Zero when the arm has no tail.
    diagnostics_reserve_seconds: int


_ARMS: Final[tuple[PollingArm, ...]] = (
    PollingArm(
        label="infra.yml:deploy",
        workflow=WORKFLOWS_DIR / "infra.yml",
        job="deploy",
        module=MODULES_DIR / "postgres-bootstrap.bicep",
        diagnostics_reserve_seconds=0,
    ),
    PollingArm(
        label="maintenance.yml:maintenance",
        workflow=WORKFLOWS_DIR / "maintenance.yml",
        job="maintenance",
        module=MODULES_DIR / "maintenance-job.bicep",
        # The tail sleeps 120 s for log ingestion, then issues an execution show,
        # a log stream, an extension install, and a Log Analytics query.
        diagnostics_reserve_seconds=600,
    ),
)

_ARM_IDS: Final[tuple[str, ...]] = tuple(arm.label for arm in _ARMS)


# ---------------------------------------------------------------------------
# Detectors (pure over text, so the controls can drive them with synthetic input)
# ---------------------------------------------------------------------------


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _poll_step_run(workflow: dict, job: str) -> str:
    """The ``run:`` script of ``job``'s step that polls a job execution."""
    steps = ((workflow.get("jobs") or {}).get(job) or {}).get("steps") or []
    for step in steps:
        run = step.get("run", "") if isinstance(step, dict) else ""
        if _POLL_MARKER in run:
            return run
    raise AssertionError(f"job `{job}` has no step running `{_POLL_MARKER}`")


def _job_timeout_minutes(workflow: dict, job: str) -> int | None:
    value = ((workflow.get("jobs") or {}).get(job) or {}).get("timeout-minutes")
    return int(value) if value is not None else None


def _replica_settings(module_text: str) -> tuple[int | None, int | None]:
    """``(replicaTimeout, replicaRetryLimit)`` declared by a job module.

    A module declaring a second job would make "the" replica settings ambiguous,
    and taking the first match would bind the budget to whichever job happens to
    appear earlier -- a wrong binding that looks exactly like a right one. Fail
    loudly instead, so splitting a module is a visible decision rather than a
    silent re-target.
    """
    timeouts = _REPLICA_TIMEOUT_RE.findall(module_text)
    retries = _REPLICA_RETRY_RE.findall(module_text)
    assert len(timeouts) < 2 and len(retries) < 2, (
        "a job module must declare exactly one replicaTimeout / replicaRetryLimit "
        f"pair to bind a poll budget to (found {len(timeouts)} / {len(retries)}); "
        "register the additional job as its own polling arm"
    )
    return (
        int(timeouts[0]) if timeouts else None,
        int(retries[0]) if retries else None,
    )


def _declared_replica_timeout(run: str) -> int | None:
    """The arm's ``REPLICA_TIMEOUT`` mirror of its module's ``replicaTimeout``."""
    found = _DECLARED_TIMEOUT_RE.search(run)
    return int(found.group(1)) if found else None


def _retry_limits(run: str) -> list[int]:
    """Every ``RETRY_LIMIT`` the arm assigns.

    An arm may pin a different limit per command, so the worst case is the
    maximum -- reading only the first occurrence would take the lowest branch
    and under-compute the budget.
    """
    return [int(v) for v in _RETRY_LIMIT_RE.findall(run)]


def _poll_slack(run: str) -> int | None:
    """The arm's allowance for the pre-replica queue wait and the terminal
    status-propagation lag; each attempt's image pull is charged against
    ``replicaTimeout`` and so is already covered by the retry-inclusive product."""
    found = _SLACK_RE.search(run)
    return int(found.group(1)) if found else None


def _poll_interval(run: str) -> int | None:
    """The poll loop's own sleep, scoped to the loop.

    A ``sleep`` in a post-loop diagnostics tail must not be mistaken for the
    poll interval -- that would compute a wrong ceiling from a legacy
    iteration-bounded loop and make the budget comparison meaningless.
    """
    loop = _ITERATION_LOOP_RE.search(run)
    if loop is None:
        return None
    found = re.search(r"sleep\s+(\d+)", run[loop.end() :])
    return int(found.group(1)) if found else None


def _eval_arithmetic(expression: str, names: dict[str, int]) -> int | None:
    """Evaluate a shell arithmetic expression over ``names``.

    Evaluated rather than pattern-matched so a budget that *looks* derived but
    drops the ``+ 1`` (or multiplies the wrong term) is caught by its value, not
    only by the shape of its source.
    """
    try:
        tree = ast.parse(expression.strip().replace("$", ""), mode="eval")
    except SyntaxError:
        return None

    def visit(node: ast.AST) -> int:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in names:
                raise ValueError(f"unknown name in budget expression: {node.id}")
            return names[node.id]
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mult, ast.Sub)):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            return left * right
        raise ValueError(f"unsupported node in budget expression: {type(node).__name__}")

    try:
        return visit(tree)
    except ValueError:
        return None


def _budget_seconds(run: str, retry_limit: int) -> int | None:
    """The arm's wall-clock poll budget at ``retry_limit``.

    Falls back to the legacy ``iterations x sleep`` product when the arm has no
    derived budget, so the failure message names the real ceiling rather than
    reporting an absent one.
    """
    budget = _BUDGET_RE.search(run)
    if budget is not None:
        timeout = _declared_replica_timeout(run)
        slack = _poll_slack(run)
        if timeout is None or slack is None:
            return None
        return _eval_arithmetic(
            budget.group(1),
            {"REPLICA_TIMEOUT": timeout, "RETRY_LIMIT": retry_limit, "POLL_SLACK": slack},
        )
    loop = _ITERATION_LOOP_RE.search(run)
    interval = _poll_interval(run)
    if loop is None or interval is None:
        return None
    return int(loop.group(1)) * interval


def _effective_retry_limit(run: str, module_retry_limit: int | None) -> int:
    """The highest retry limit the arm can run under: the deployed default, or
    any higher limit the arm re-pins per dispatch."""
    candidates = _retry_limits(run) + (
        [module_retry_limit] if module_retry_limit is not None else []
    )
    return max(candidates) if candidates else 0


def _iter_workflow_files() -> list[Path]:
    return sorted(
        p for p in WORKFLOWS_DIR.iterdir() if p.is_file() and p.suffix in (".yml", ".yaml")
    )


def _polling_arm_labels(workflow: dict, label: str) -> set[str]:
    """``{"<label>:<job>"}`` for each job with a step that polls a job execution."""
    found: set[str] = set()
    for name, job in (workflow.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            run = step.get("run", "") if isinstance(step, dict) else ""
            if _POLL_MARKER in run:
                found.add(f"{label}:{name}")
                break
    return found


def _discover_polling_arms() -> set[str]:
    """Every job-polling arm across every committed workflow."""
    found: set[str] = set()
    for path in _iter_workflow_files():
        found |= _polling_arm_labels(_load(path), path.name)
    return found


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("arm", _ARMS, ids=_ARM_IDS)
def test_poll_budget_is_wall_clock_not_iteration_count(arm: PollingArm) -> None:
    """The wait is bounded by a deadline, not by a count of iterations.

    An iteration bound silently costs the status call's latency on every pass, so
    the loop's nominal ceiling is never reached: the runner's own timeout fires
    first, mid-loop, and whatever follows the loop never runs.
    """
    run = _poll_step_run(_load(arm.workflow), arm.job)
    assert _DEADLINE_ASSIGN_RE.search(run), (
        f"{arm.label}: the poll wait must compute a wall-clock deadline "
        "(POLL_DEADLINE from `date +%s` plus POLL_BUDGET)"
    )
    assert _DEADLINE_LOOP_RE.search(run), (
        f"{arm.label}: the poll loop must run until the wall-clock deadline"
    )
    assert not _ITERATION_LOOP_RE.search(run), (
        f"{arm.label}: the poll loop must not be bounded by an iteration count -- "
        "per-iteration `az` latency erodes the budget by an unbounded amount"
    )


@pytest.mark.parametrize("arm", _ARMS, ids=_ARM_IDS)
def test_declared_replica_timeout_matches_the_module(arm: PollingArm) -> None:
    """The arm's replica-timeout mirror equals the module's declaration, so
    neither number can move without the other."""
    run = _poll_step_run(_load(arm.workflow), arm.job)
    declared = _declared_replica_timeout(run)
    module_timeout, _ = _replica_settings(arm.module.read_text(encoding="utf-8"))

    assert module_timeout is not None, f"{arm.module.name} must declare a replicaTimeout"
    assert declared is not None, (
        f"{arm.label}: the poll wait must declare REPLICA_TIMEOUT, mirroring "
        f"{arm.module.name}'s replicaTimeout"
    )
    assert declared == module_timeout, (
        f"{arm.label}: REPLICA_TIMEOUT ({declared}s) has drifted from "
        f"{arm.module.name}'s replicaTimeout ({module_timeout}s)"
    )


@pytest.mark.parametrize("arm", _ARMS, ids=_ARM_IDS)
def test_budget_is_derived_from_the_replica_settings(arm: PollingArm) -> None:
    """The budget is computed from the replica settings, not hand-set.

    A hand-set constant is what decouples from the module in the first place;
    the derivation is what makes the drift check above bite.
    """
    run = _poll_step_run(_load(arm.workflow), arm.job)
    budget = _BUDGET_RE.search(run)
    assert budget, f"{arm.label}: the poll wait must compute POLL_BUDGET"

    expression = budget.group(1)
    for name in ("REPLICA_TIMEOUT", "RETRY_LIMIT", "POLL_SLACK"):
        assert name in expression, (
            f"{arm.label}: POLL_BUDGET must be derived from {name}, not hand-set "
            f"(got `{expression.strip()}`)"
        )

    slack = _poll_slack(run)
    assert slack is not None and slack > 0, (
        f"{arm.label}: POLL_SLACK must be positive -- the queue wait before the "
        "replica and the status-propagation lag after termination fall outside "
        "replicaTimeout"
    )


@pytest.mark.parametrize("arm", _ARMS, ids=_ARM_IDS)
def test_poll_budget_covers_a_retried_execution(arm: PollingArm) -> None:
    """The budget outlasts a fully retried execution.

    ``replicaTimeout`` bounds one replica; a job with a retry limit can spend
    that window once per attempt. A budget covering only a single attempt
    reports a false failure mid-retry and abandons the running execution.
    """
    run = _poll_step_run(_load(arm.workflow), arm.job)
    module_timeout, module_retry = _replica_settings(arm.module.read_text(encoding="utf-8"))
    assert module_timeout is not None, f"{arm.module.name} must declare a replicaTimeout"

    retry_limit = _effective_retry_limit(run, module_retry)
    required = module_timeout * (retry_limit + 1)
    budget = _budget_seconds(run, retry_limit)

    assert budget is not None, f"{arm.label}: the poll wait declares no readable budget"
    assert budget >= required, (
        f"{arm.label}: the poll budget ({budget}s) must cover a fully retried "
        f"execution ({module_timeout}s x {retry_limit + 1} attempts = {required}s), "
        "or a legitimate retry is reported as a failure"
    )


@pytest.mark.parametrize("arm", _ARMS, ids=_ARM_IDS)
def test_workflow_retry_limit_not_below_the_deployed_default(arm: PollingArm) -> None:
    """The arm budgets for at least the retry limit the job is deployed with.

    An arm that re-pins the limit per dispatch (CAS-ADR-029 holds the
    destructive commands at no auto-retry) may raise it; an arm that only
    mirrors the module must match it exactly.
    """
    run = _poll_step_run(_load(arm.workflow), arm.job)
    _, module_retry = _replica_settings(arm.module.read_text(encoding="utf-8"))
    assert module_retry is not None, f"{arm.module.name} must declare a replicaRetryLimit"

    limits = _retry_limits(run)
    assert limits, f"{arm.label}: the poll wait must declare RETRY_LIMIT"

    if _RETRY_REPIN_MARKER in run:
        assert max(limits) >= module_retry, (
            f"{arm.label}: the highest re-pinned RETRY_LIMIT ({max(limits)}) is below "
            f"{arm.module.name}'s deployed replicaRetryLimit ({module_retry})"
        )
    else:
        assert limits == [module_retry], (
            f"{arm.label}: RETRY_LIMIT {limits} must mirror {arm.module.name}'s "
            f"replicaRetryLimit ({module_retry}) -- the arm does not re-pin it"
        )


@pytest.mark.parametrize("arm", _ARMS, ids=_ARM_IDS)
def test_job_timeout_outlasts_budget_and_diagnostics(arm: PollingArm) -> None:
    """The runner's job timeout outlasts the budget plus the arm's diagnostics
    reserve, so a timed-out poll reports its own failure -- with whatever
    diagnostics follow the loop -- instead of being killed mid-loop."""
    workflow = _load(arm.workflow)
    run = _poll_step_run(workflow, arm.job)
    _, module_retry = _replica_settings(arm.module.read_text(encoding="utf-8"))

    budget = _budget_seconds(run, _effective_retry_limit(run, module_retry))
    assert budget is not None, f"{arm.label}: the poll wait declares no readable budget"

    timeout_minutes = _job_timeout_minutes(workflow, arm.job)
    assert timeout_minutes is not None, f"{arm.label}: the job must declare timeout-minutes"

    required = budget + arm.diagnostics_reserve_seconds
    assert timeout_minutes * 60 > required, (
        f"{arm.label}: timeout-minutes ({timeout_minutes} = {timeout_minutes * 60}s) must "
        f"outlast the poll budget ({budget}s) plus its diagnostics reserve "
        f"({arm.diagnostics_reserve_seconds}s)"
    )


def test_every_polling_arm_is_registered() -> None:
    """Every job-polling arm in the repository is covered.

    Discovery rather than a hand-kept list: a new workflow that waits on a job
    execution would otherwise carry the whole defect class outside this gate,
    and a stale entry for a removed arm would make the checks above vacuous.
    """
    discovered = _discover_polling_arms()
    registered = {arm.label for arm in _ARMS}
    assert discovered == registered, (
        "every job-polling workflow arm must be registered in this gate "
        f"(unregistered: {sorted(discovered - registered)}; "
        f"stale: {sorted(registered - discovered)})"
    )


# ---------------------------------------------------------------------------
# Anti-coincidental-pass controls
#
# Prove each detector fires on the regression it exists to catch, NOT that the
# committed workflows happen to satisfy it. A numeric gate whose extractor
# returns nothing would otherwise pass every arm vacuously.
# ---------------------------------------------------------------------------


_SYNTHETIC_DEADLINE_RUN: Final[str] = (
    "set -euo pipefail\n"
    "REPLICA_TIMEOUT=600\n"
    "RETRY_LIMIT=1\n"
    "POLL_SLACK=300\n"
    "POLL_BUDGET=$(( REPLICA_TIMEOUT * (RETRY_LIMIT + 1) + POLL_SLACK ))\n"
    "POLL_DEADLINE=$(( $(date +%s) + POLL_BUDGET ))\n"
    'while [ "$(date +%s)" -lt "$POLL_DEADLINE" ]; do\n'
    "  az containerapp job execution show --query properties.status\n"
    "  sleep 10\n"
    "done\n"
)

_SYNTHETIC_ITERATION_RUN: Final[str] = (
    "set -euo pipefail\n"
    "for _ in $(seq 1 60); do\n"
    "  az containerapp job execution show --query properties.status\n"
    "  sleep 10\n"
    "done\n"
    "echo 'diagnostics'\n"
    "sleep 120\n"
)


def test_detector_reds_on_an_under_budgeted_arm() -> None:
    """A budget that drops the retry term is caught by its *value*, so a
    derivation that merely looks right cannot pass."""
    run = _SYNTHETIC_DEADLINE_RUN.replace(
        "REPLICA_TIMEOUT * (RETRY_LIMIT + 1) + POLL_SLACK", "REPLICA_TIMEOUT + POLL_SLACK"
    )
    budget = _budget_seconds(run, retry_limit=1)
    assert budget == 900
    assert budget < 600 * 2, "the under-budgeted arm must fall short of the retried worst case"


def test_detector_reds_on_an_iteration_bounded_loop() -> None:
    """The iteration-bound forbidden by the wall-clock check is detected, and the
    deadline form it must be replaced by is not falsely detected in it."""
    assert _ITERATION_LOOP_RE.search(_SYNTHETIC_ITERATION_RUN)
    assert not _DEADLINE_ASSIGN_RE.search(_SYNTHETIC_ITERATION_RUN)
    assert not _DEADLINE_LOOP_RE.search(_SYNTHETIC_ITERATION_RUN)
    # ...and the accepted form clears both halves.
    assert not _ITERATION_LOOP_RE.search(_SYNTHETIC_DEADLINE_RUN)
    assert _DEADLINE_ASSIGN_RE.search(_SYNTHETIC_DEADLINE_RUN)
    assert _DEADLINE_LOOP_RE.search(_SYNTHETIC_DEADLINE_RUN)


def test_detector_reds_when_the_module_timeout_moves() -> None:
    """The mirror check compares two independently-parsed numbers, so a module
    edit alone breaks it -- it is not a self-comparison."""
    module_timeout, module_retry = _replica_settings(
        "configuration: {\n  replicaTimeout: 900\n  replicaRetryLimit: 2\n}"
    )
    assert (module_timeout, module_retry) == (900, 2)
    assert _declared_replica_timeout(_SYNTHETIC_DEADLINE_RUN) == 600
    assert _declared_replica_timeout(_SYNTHETIC_DEADLINE_RUN) != module_timeout


def test_detector_reds_on_a_module_declaring_two_jobs() -> None:
    """A module carrying a second job is rejected rather than bound to whichever
    job appears first -- a wrong binding is indistinguishable from a right one at
    every later assertion, so it has to fail here."""
    with pytest.raises(AssertionError, match="exactly one replicaTimeout"):
        _replica_settings(
            "configuration: {\n  replicaTimeout: 600\n  replicaRetryLimit: 1\n}\n"
            "configuration: {\n  replicaTimeout: 3600\n  replicaRetryLimit: 0\n}\n"
        )


def test_detector_reds_when_timeout_minutes_is_too_small() -> None:
    """A job timeout below the budget plus its reserve is caught."""
    workflow = yaml.safe_load(
        "jobs:\n"
        "  poll:\n"
        "    timeout-minutes: 20\n"
        "    steps:\n"
        "      - run: az containerapp job execution show\n"
    )
    assert _job_timeout_minutes(workflow, "poll") == 20
    assert _job_timeout_minutes(workflow, "poll") * 60 < 1500


def test_retry_limits_reads_every_assignment() -> None:
    """Every ``RETRY_LIMIT`` assignment is read.

    An arm pins a lower limit for its destructive commands and raises it for the
    resumable sweep; reading only the first would budget for the wrong branch.
    """
    run = "RETRY_LIMIT=0\ncase x in\n  sweep)\n    RETRY_LIMIT=1 ;;\nesac\n"
    assert _retry_limits(run) == [0, 1]
    assert _effective_retry_limit(run, module_retry_limit=0) == 1
    # The deployed default wins when it is the higher of the two.
    assert _effective_retry_limit("RETRY_LIMIT=0\n", module_retry_limit=1) == 1


def test_poll_interval_scopes_to_the_loop() -> None:
    """The poll interval is read from the loop, not from a later diagnostics
    sleep -- which would compute a wrong legacy ceiling and let the budget
    comparison pass on a number the loop never had."""
    assert _poll_interval(_SYNTHETIC_ITERATION_RUN) == 10
    assert _budget_seconds(_SYNTHETIC_ITERATION_RUN, retry_limit=1) == 600


def test_discovery_finds_a_synthetic_polling_arm() -> None:
    """The scanner actually fires, rather than returning an empty set that would
    make the registration check pass against an empty registry."""
    workflow = yaml.safe_load(
        "jobs:\n"
        "  waiter:\n"
        "    steps:\n"
        "      - run: |\n"
        "          az containerapp job execution show --query properties.status\n"
        "  bystander:\n"
        "    steps:\n"
        "      - run: echo hi\n"
    )
    assert _polling_arm_labels(workflow, "synthetic.yml") == {"synthetic.yml:waiter"}


def test_scan_reaches_all_workflow_files() -> None:
    """The scan visits every committed workflow, so a real one cannot be silently
    excluded from discovery."""
    scanned = {p.name for p in _iter_workflow_files()}
    expected = {"infra.yml", "ci.yml", "build-images.yml", "maintenance.yml"}
    assert expected <= scanned, f"poll-budget scan must reach {sorted(expected - scanned)}"
