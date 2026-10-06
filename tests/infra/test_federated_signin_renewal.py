"""Gates for renewing the federated Azure sign-in inside long-running steps.

A federated sign-in stores the client assertion it was made with, and the Azure
CLI re-presents it whenever it mints a token: for a new scope, or to replace an
expired cached token. Entra accepts the assertion for only a few minutes, so a
step that waits on an in-VNet job for an hour cannot rely on the sign-in its job
opened with. ``deploy/azure-federated-signin.sh --if-stale`` renews it; these
gates hold that every such wait calls it where it matters, and that the jobs
running it carry the identity coordinates it needs.

The checks read the tracked workflow YAML. Each detector is a pure function over
text, so the controls below drive it with the defect it exists to catch.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
import yaml

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
WORKFLOWS: Final[Path] = REPO_ROOT / ".github" / "workflows"
RENEW: Final[str] = "bash deploy/azure-federated-signin.sh --if-stale"
STATUS_CALL: Final[str] = "az containerapp job execution show"
LOG_QUERY: Final[str] = "az monitor log-analytics query"
COORDINATES: Final[dict[str, str]] = {
    "AZURE_CLIENT_ID": "${{ vars.AZURE_CLIENT_ID }}",
    "AZURE_TENANT_ID": "${{ vars.AZURE_TENANT_ID }}",
    "AZURE_SUBSCRIPTION_ID": "${{ vars.AZURE_SUBSCRIPTION_ID }}",
}


def _job(workflow: str, job: str) -> dict:
    loaded = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    return loaded["jobs"][job]


def _commands(run: str) -> list[str]:
    """The script's non-comment lines, continuation lines joined into one command."""
    commands: list[str] = []
    pending = ""
    for raw in run.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pending = f"{pending} {line}".strip() if pending else line
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        commands.append(pending)
        pending = ""
    if pending:
        commands.append(pending)
    return commands


def _poll_loop_problem(run: str) -> str | None:
    """Why a polling loop does not renew the sign-in before each status call."""
    commands = _commands(run)
    loops = [i for i, c in enumerate(commands) if c.startswith("while ")]
    if not loops:
        return "no polling loop found"
    for start in loops:
        end = next((i for i in range(start, len(commands)) if commands[i] == "done"), None)
        if end is None:
            return "polling loop is not closed"
        body = commands[start + 1 : end]
        status = next((i for i, c in enumerate(body) if STATUS_CALL in c), None)
        if status is None:
            continue
        if not any(c == RENEW for c in body[:status]):
            return f"the polling loop must run `{RENEW}` before each `{STATUS_CALL}`"
    return None


def _log_query_problem(run: str) -> str | None:
    """Why a Log Analytics query after a wait does not follow a renewal."""
    commands = _commands(run)
    query = next((i for i, c in enumerate(commands) if c.startswith(LOG_QUERY)), None)
    if query is None:
        return "no Log Analytics query found"
    preceding = commands[:query]
    last_wait = max((i for i, c in enumerate(preceding) if c.startswith("sleep ")), default=-1)
    if not any(c == RENEW for c in preceding[last_wait + 1 :]):
        return f"`{RENEW}` must run after the last wait and before `{LOG_QUERY}`"
    return None


def _coordinates_problem(job: dict) -> str | None:
    env = job.get("env") or {}
    missing = {k: v for k, v in COORDINATES.items() if env.get(k) != v}
    if missing:
        return f"the job env must carry the deploy identity coordinates: {sorted(missing)}"
    return None


def _maintenance_wait_run() -> str:
    for step in _job("maintenance.yml", "maintenance")["steps"]:
        if STATUS_CALL in step.get("run", ""):
            return step["run"]
    raise AssertionError("the maintenance job has no polling step")


def test_maintenance_poll_loop_renews_the_sign_in() -> None:
    problem = _poll_loop_problem(_maintenance_wait_run())
    assert problem is None, problem


def test_maintenance_log_query_follows_a_renewal() -> None:
    problem = _log_query_problem(_maintenance_wait_run())
    assert problem is None, problem


@pytest.mark.parametrize(
    ("workflow", "job"),
    [("maintenance.yml", "maintenance"), ("postgres-migration.yml", "migration")],
)
def test_renewing_jobs_carry_the_identity_coordinates(workflow: str, job: str) -> None:
    """The renewal helper fails loudly in CI without these, so a job that renews
    must expose them to its steps -- the maintenance loop directly, the migration
    driver through its Azure boundary before every call.
    """
    problem = _coordinates_problem(_job(workflow, job))
    assert problem is None, problem


def test_migration_job_runs_the_driver_that_renews() -> None:
    """The migration job's Azure work runs through the driver whose boundary renews."""
    runs = [s.get("run", "") for s in _job("postgres-migration.yml", "migration")["steps"]]
    assert sum("bash deploy/postgres-migration.sh" in r for r in runs) == 2, (
        "both the migration step and the report retrieval must run through the driver"
    )
    driver = (REPO_ROOT / "deploy" / "postgres-migration.py").read_text(encoding="utf-8")
    assert re.search(r'SIGNIN = ROOT / "deploy" / "azure-federated-signin\.sh"', driver)


_LOOP = """\
set -euo pipefail
while [ "$(date +%s)" -lt "$POLL_DEADLINE" ]; do
  {renew_before}
  STATUS="$(az containerapp job execution show \\
    --name "$JOB" --query properties.status -o tsv)"
  {renew_after}
  sleep 10
done
if [ "$OK" != true ]; then
  {renew_diag}
  sleep 120
  {renew_query}
  az monitor log-analytics query --workspace "$WS" \\
    --analytics-query "x" --output table || true
fi
"""


def _loop(**overrides: str) -> str:
    parts = {
        "renew_before": RENEW,
        "renew_after": "",
        "renew_diag": "",
        "renew_query": RENEW,
    }
    parts.update(overrides)
    return _LOOP.format(**parts)


def test_detectors_accept_the_correct_shape() -> None:
    assert _poll_loop_problem(_loop()) is None
    assert _log_query_problem(_loop()) is None


@pytest.mark.parametrize(
    ("label", "script"),
    [
        ("renewal missing from the loop", _loop(renew_before="")),
        ("renewal after the status call", _loop(renew_before="", renew_after=RENEW)),
        ("renewal only in a comment", _loop(renew_before=f"# {RENEW}")),
        ("renewal without --if-stale", _loop(renew_before="bash deploy/azure-federated-signin.sh")),
    ],
)
def test_poll_loop_detector_rejects(label: str, script: str) -> None:
    assert _poll_loop_problem(script) is not None, label


@pytest.mark.parametrize(
    ("label", "script"),
    [
        ("renewal missing before the query", _loop(renew_query="")),
        ("renewal only before the ingestion wait", _loop(renew_query="", renew_diag=RENEW)),
    ],
)
def test_log_query_detector_rejects(label: str, script: str) -> None:
    assert _log_query_problem(script) is not None, label


def test_coordinates_detector_rejects_a_missing_or_wrong_coordinate() -> None:
    assert _coordinates_problem({"env": dict(COORDINATES)}) is None
    for key in COORDINATES:
        missing = {k: v for k, v in COORDINATES.items() if k != key}
        assert _coordinates_problem({"env": missing}) is not None
        wrong = {**COORDINATES, key: "${{ vars.SOMETHING_ELSE }}"}
        assert _coordinates_problem({"env": wrong}) is not None
