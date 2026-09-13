"""The change-record gate runs in CI, against the change's own base.

``scripts/substrate_changes.py check`` is only a gate if a required job runs it
on every change, and only a correct one if it compares the change against the
revision the change was actually merged with. Nothing about a green run shows
either: a step that was removed, conditioned away, or handed the wrong base
passes exactly as a working one does. This file reads the workflow and holds
both properties.

The gate lives in the ``test`` job, which the branch ruleset already requires,
so adding it needed no change to the required checks. Its base resolution
mirrors the ``test-surface`` job's, and the arm coverage below is derived from
the workflow's own trigger list for the same reason that job's gate derives
it: a curated list passes on the day it is written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import yaml

from tests.infra.test_ci_test_surface_job import case_arms, step_script, workflow_triggers

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
CI_WORKFLOW: Final[Path] = REPO_ROOT / ".github" / "workflows" / "ci.yml"
TEST_JOB: Final[str] = "test"
BASE_STEP_ID: Final[str] = "substrate-base"
CHECK_COMMAND: Final[str] = "scripts.substrate_changes check"


def _test_job() -> dict[str, Any]:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"][TEST_JOB]


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step or {} for step in job.get("steps") or []]


def _index(job: dict[str, Any], fragment: str) -> list[int]:
    return [i for i, step in enumerate(_steps(job)) if fragment in str(step.get("run") or "")]


def test_the_required_test_job_runs_the_gate_before_the_suite() -> None:
    job = _test_job()
    checks, suites = _index(job, CHECK_COMMAND), _index(job, "pytest")

    assert len(checks) == 1, (
        f"expected one change-record gate step in {TEST_JOB}, found {len(checks)}"
    )
    assert len(suites) == 1
    assert checks[0] < suites[0], "the gate runs after the suite; a missing record reports late"


def test_the_gate_is_given_the_resolved_base() -> None:
    job = _test_job()
    step = _steps(job)[_index(job, CHECK_COMMAND)[0]]

    assert step.get("if") == f"steps.{BASE_STEP_ID}.outputs.sha != ''"
    assert (step.get("env") or {}).get("BASE_SHA") == f"${{{{ steps.{BASE_STEP_ID}.outputs.sha }}}}"
    assert '--base "$BASE_SHA"' in step["run"]


def test_the_base_resolution_covers_every_trigger() -> None:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    script = step_script(workflow["jobs"][TEST_JOB], BASE_STEP_ID)

    assert script, f"no step with id {BASE_STEP_ID!r} in {TEST_JOB}"
    assert workflow_triggers(workflow) <= case_arms(script)
    assert "HEAD^1" in script, "merged-tree events must compare against the merge's first parent"


def test_the_checkout_carries_history() -> None:
    """A shallow checkout has no base commit to compare against, and no tags."""
    job = _test_job()
    checkout = next(s for s in _steps(job) if str(s.get("uses", "")).startswith("actions/checkout"))

    assert (checkout.get("with") or {}).get("fetch-depth") == 0
