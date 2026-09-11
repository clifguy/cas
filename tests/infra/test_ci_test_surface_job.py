"""The test-surface job must measure the same suite the `test` job runs.

``scripts/check_test_surface.py`` compares which tests would actually run,
before and after a change. That set is a property of the tree *and* of the
environment: a class gated behind an opt-in is collected everywhere and runs
only where the opt-in is set. So a comparison made under different opt-ins than
the suite whose coverage is at stake describes a suite nobody runs, and reports
a clean result while the real one contracts -- which is the failure it was
built to catch, recurring one level up on the check itself.

Nothing about either job's own run reveals that drift. Both pass. The `test`
job does not read the surface job's environment and the surface job does not
read the `test` job's, so the two definitions can diverge in a one-line edit
that looks entirely local. This gate is the only place the agreement is stated.

The second check covers the other way the job can go quiet: a base revision it
cannot resolve. The comparison is skipped when there is nothing to compare
against, so an event whose arm went missing would skip on every run and report
success having measured nothing.

Anti-coincidental controls accompany both: a "these must agree" assertion whose
extractor silently returned nothing would pass against any pair of jobs, and an
arm-coverage assertion whose parser found no arms would pass against a workflow
with no resolution step at all.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
CI_WORKFLOW: Final[Path] = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The job that runs the suite, and the job that measures which tests that run
# would have included.
TEST_JOB: Final[str] = "test"
SURFACE_JOB: Final[str] = "test-surface"

# Environment variables that decide whether a test runs rather than how it
# behaves once running. Every one of these is read by a ``skipif`` somewhere
# under ``tests/``, so a difference between the two jobs moves tests in or out
# of the measured set. A variable absent from one job and present in the other
# is a difference; both absent is agreement.
SKIP_GATING_ENV: Final[tuple[str, ...]] = (
    "SAGE_TEST_PG_DSN",
    "SAGE_TEST_STUB_PROVIDERS",
    "SAGE_TEST_REAL_MODELS",
    "SAGE_TEST_DOCKER",
)

# The events ``ci.yml`` answers, each of which needs its own way of naming the
# revision the change departs from.
EXPECTED_BASE_ARMS: Final[frozenset[str]] = frozenset({"pull_request", "merge_group", "push"})

# The step that resolves that revision, addressed by its id rather than its
# name so rewording the name does not silently empty this gate.
BASE_STEP_ID: Final[str] = "base"

# A ``case`` arm label. Anchored to the start of a line, with the closing
# paren required immediately after the label, so a subshell or a glob inside an
# arm body cannot be mistaken for an arm of its own. The body may follow on the
# same line, because a one-line arm is the ordinary form here.
_CASE_ARM_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t]*([A-Za-z_*][A-Za-z0-9_|*]*)\)(?=[ \t]|$)", re.MULTILINE
)


# ---------------------------------------------------------------------------
# Extractors (pure functions over a parsed workflow — exercised by the controls)
# ---------------------------------------------------------------------------


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _job(workflow: dict[str, Any], name: str) -> dict[str, Any]:
    jobs = workflow.get("jobs") or {}
    assert name in jobs, f"ci.yml has no {name!r} job; this gate is measuring nothing"
    return jobs[name] or {}


def skip_gating_env(job: dict[str, Any], step: dict[str, Any]) -> dict[str, str | None]:
    """Each skip-gating variable as ``step`` sees it, ``None`` when unset.

    Job-level ``env`` overlaid with the step's own, which is how GitHub
    resolves it. Both levels have to be read: the job that runs the suite
    declares its database and provider settings on the pytest step, while the
    job that measures the surface declares them once for the job.
    """
    declared = {**(job.get("env") or {}), **((step or {}).get("env") or {})}
    return {name: declared.get(name) for name in SKIP_GATING_ENV}


def steps_running(job: dict[str, Any], fragment: str) -> list[dict[str, Any]]:
    """Every step whose ``run:`` body contains ``fragment``."""
    return [
        step for step in (job.get("steps") or []) if fragment in str((step or {}).get("run") or "")
    ]


def case_arms(script: str) -> set[str]:
    """The event labels a shell ``case`` block branches on.

    ``a|b)`` counts as both. The catch-all ``*)`` is dropped: it is the arm
    that yields no base, so counting it would let it stand in for a real event.
    """
    arms: set[str] = set()
    for label in _CASE_ARM_RE.findall(script):
        arms.update(part for part in label.split("|") if part != "*")
    return arms


def step_script(job: dict[str, Any], step_id: str) -> str:
    """The ``run:`` body of the step carrying ``step_id``."""
    for step in job.get("steps") or []:
        if (step or {}).get("id") == step_id:
            return str(step.get("run") or "")
    return ""


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------


def _running_environment() -> dict[str, str | None]:
    """The skip-gating environment the suite actually runs under."""
    job = _job(_load(CI_WORKFLOW), TEST_JOB)
    steps = steps_running(job, "pytest")
    assert len(steps) == 1, f"expected one pytest step on the {TEST_JOB} job, found {len(steps)}"
    return skip_gating_env(job, steps[0])


def _measuring_environments() -> list[tuple[str, dict[str, str | None]]]:
    """The skip-gating environment of each side of the comparison.

    Both emit steps are read, not just the one measuring this revision: the
    step measuring the base runs its own shell in another checkout, and an
    override added to one and not the other would make the two halves of the
    comparison incomparable.
    """
    job = _job(_load(CI_WORKFLOW), SURFACE_JOB)
    steps = steps_running(job, "--emit")
    assert len(steps) == 2, f"expected two emit steps on the {SURFACE_JOB} job, found {len(steps)}"
    return [(str(step.get("name") or "?"), skip_gating_env(job, step)) for step in steps]


def test_the_surface_job_measures_the_test_job_environment() -> None:
    """Every measurement must see the same skip-gating variables the suite runs under."""
    running = _running_environment()

    for label, measured in _measuring_environments():
        divergent = {
            name: {"test job": running[name], label: measured[name]}
            for name in SKIP_GATING_ENV
            if running[name] != measured[name]
        }
        assert not divergent, (
            f"the {SURFACE_JOB!r} step {label!r} measures a different environment "
            f"than the {TEST_JOB!r} job runs, on {sorted(divergent)}: {divergent}. A "
            "test gated on one of these would count as running on one side and not "
            "the other, so the comparison would describe a suite nobody runs."
        )


def test_the_environment_comparison_is_not_vacuous() -> None:
    """At least one skip-gating variable must actually be set on the test job.

    An extractor that returned every value as ``None`` would make the agreement
    above hold against any pair of jobs, including two that share nothing.
    """
    running = _running_environment()
    assert any(value is not None for value in running.values()), (
        "no skip-gating variable found on the test job; the agreement assertion "
        "would pass against any workflow"
    )


def test_the_surface_job_resolves_a_base_for_every_ci_event() -> None:
    """Each event ci.yml answers needs its own arm naming a base revision.

    An event with no arm falls to the catch-all, which yields no base, which
    skips the comparison — a job that reports success having measured nothing.
    """
    surface = _job(_load(CI_WORKFLOW), SURFACE_JOB)
    arms = case_arms(step_script(surface, BASE_STEP_ID))

    missing = sorted(EXPECTED_BASE_ARMS - arms)
    assert not missing, (
        f"the base-resolution step has no arm for {missing}; a {missing} run would "
        "skip the comparison and report success having measured nothing"
    )


def test_the_surface_job_compares_only_when_a_base_resolved() -> None:
    """The comparison step must be conditional on the resolved base.

    Unconditional, it would fail on a missing file for every event that has no
    base — turning "nothing to compare" into an error, which trains the reader
    to ignore the job.
    """
    surface = _job(_load(CI_WORKFLOW), SURFACE_JOB)
    conditions = [
        str((step or {}).get("if") or "")
        for step in surface.get("steps") or []
        if "--compare" in str((step or {}).get("run") or "")
    ]
    assert conditions, "no step in the test-surface job runs the comparison"
    for condition in conditions:
        assert f"steps.{BASE_STEP_ID}.outputs.sha" in condition, (
            f"the comparison step is not gated on a resolved base: {condition!r}"
        )


# ---------------------------------------------------------------------------
# Anti-coincidental controls on the extractors
# ---------------------------------------------------------------------------

_SYNTHETIC_DIVERGENT_JOBS: Final[str] = textwrap.dedent(
    """
    jobs:
      test:
        env:
          SAGE_TEST_STUB_PROVIDERS: "1"
        steps:
          - name: Run pytest
            env:
              SAGE_TEST_PG_DSN: "postgresql://localhost/x"
            run: pytest tests/
      test-surface:
        env:
          SAGE_TEST_STUB_PROVIDERS: "1"
        steps:
          - name: Measure
            run: check --emit
    """
)

_SYNTHETIC_CASE_BLOCK: Final[str] = textwrap.dedent(
    """
    case "$EVENT_NAME" in
      pull_request) sha="$A" ;;
      merge_group|push)
        sha=$(printf '%s' "$B")
        ;;
      *) sha="" ;;
    esac
    """
)


def test_the_environment_extractor_reports_a_real_divergence() -> None:
    """A step-level variable on one job, absent from the other, must be reported.

    Also pins the overlay itself: the variable that diverges is declared on the
    step, the one that agrees is declared on the job, so an extractor reading
    only one of the two levels fails here.
    """
    jobs = yaml.safe_load(_SYNTHETIC_DIVERGENT_JOBS)["jobs"]
    running = skip_gating_env(jobs["test"], steps_running(jobs["test"], "pytest")[0])
    measured = skip_gating_env(
        jobs["test-surface"], steps_running(jobs["test-surface"], "--emit")[0]
    )

    assert running["SAGE_TEST_PG_DSN"] != measured["SAGE_TEST_PG_DSN"]
    assert measured["SAGE_TEST_PG_DSN"] is None
    assert running["SAGE_TEST_STUB_PROVIDERS"] == measured["SAGE_TEST_STUB_PROVIDERS"] == "1"


def test_the_arm_parser_reads_alternates_and_drops_the_catch_all() -> None:
    """``a|b)`` counts as two arms; ``*)`` counts as none."""
    assert case_arms(_SYNTHETIC_CASE_BLOCK) == {"pull_request", "merge_group", "push"}


@pytest.mark.parametrize("script", ["", "sha=$PR_BASE_SHA", "echo '*)'"])
def test_the_arm_parser_finds_nothing_without_a_case_block(script: str) -> None:
    """A step that branches on nothing yields no arms, so the gate above bites."""
    assert case_arms(script) == set()
