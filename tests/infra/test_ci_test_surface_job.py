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

Both checks read the workflow rather than a restatement of it. The environment
comparison runs over every `SAGE_TEST_*` name either job sets, in both
directions, and the arm coverage is derived from the workflow's own trigger
list. A curated constant on either side passes on the day it is written and
keeps passing once the thing it mirrors moves, which is the failure these
gates exist to catch, turned on the gates themselves.

Anti-coincidental controls accompany both: an extractor that silently returned
nothing would make "these must agree" hold against any pair of jobs, a parser
that found no arms would pass against a workflow with no resolution step, and
a trigger reader blind to the boolean key YAML gives a bare `on:` would compare
against an empty set on every real workflow.
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

# Prefix of every variable the suite reads to decide which tests run at all --
# which server to reach, which providers to stub, which opt-in tiers are in.
#
# The comparison below is over every name carrying this prefix that either job
# sets, in both directions, rather than over a curated list of them. A list was
# the first form and it was wrong twice over on the day it was written: it
# omitted one live opt-in gate, and it claimed all four of its entries were
# skip gates when one is a default set in the root conftest. Sixteen distinct
# names carry the prefix under ``tests/`` today, so any list short of all of
# them is a list that goes stale silently -- which is this gate's own failure
# mode turned on itself.
#
# The prefix is the scope on purpose: variables outside it (offline flags,
# buffering) change how a test behaves once running, not whether it runs, and
# holding the two jobs to those would report drift that costs the measurement
# nothing.
SKIP_GATING_PREFIX: Final[str] = "SAGE_TEST_"

# The comparison must see at least this many names, or an extractor returning
# nothing would make the agreement below hold against any pair of jobs.
MIN_COMPARED_ENV: Final[int] = 1

# The step that resolves the base revision, addressed by its id rather than its
# name so rewording the name does not silently empty this gate.
BASE_STEP_ID: Final[str] = "base"

# The exact condition every step that reads the base measurement must carry.
# Compared whole rather than searched for, because a condition of the opposite
# polarity names the same output and would satisfy a containment check while
# running the step precisely when there is nothing to compare.
EXPECTED_BASE_CONDITION: Final[str] = f"steps.{BASE_STEP_ID}.outputs.sha != ''"

# Substring identifying a step that reads the base checkout or its measurement.
# The head-emit step writes ``head.json`` and does not match.
BASE_DEPENDENT_MARKER: Final[str] = "RUNNER_TEMP}/base"

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


def skip_gating_env(job: dict[str, Any], step: dict[str, Any]) -> dict[str, str]:
    """Every skip-gating variable ``step`` sees, and its value.

    Job-level ``env`` overlaid with the step's own, which is how GitHub
    resolves it. Both levels have to be read: the job that runs the suite
    declares its database and provider settings on the pytest step, while the
    job that measures the surface declares them once for the job.
    """
    declared = {**(job.get("env") or {}), **((step or {}).get("env") or {})}
    return {
        name: str(value) for name, value in declared.items() if name.startswith(SKIP_GATING_PREFIX)
    }


def steps_running(job: dict[str, Any], fragment: str) -> list[dict[str, Any]]:
    """Every step whose ``run:`` body contains ``fragment``."""
    return [
        step for step in (job.get("steps") or []) if fragment in str((step or {}).get("run") or "")
    ]


def workflow_triggers(workflow: dict[str, Any]) -> set[str]:
    """The event names a workflow answers.

    YAML 1.1 reads a bare ``on:`` key as the boolean ``True``, so the mapping
    arrives under that key rather than under the string. Both spellings are
    accepted; a quoted ``"on":`` is legal and some workflows use it.
    """
    triggers = workflow.get(True, workflow.get("on"))
    assert isinstance(triggers, dict) and triggers, (
        f"could not read the workflow's triggers (got {triggers!r}); the arm gate "
        "would compare against nothing"
    )
    return set(triggers)


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


def _running_environment() -> dict[str, str]:
    """The skip-gating environment the suite actually runs under."""
    job = _job(_load(CI_WORKFLOW), TEST_JOB)
    steps = steps_running(job, "pytest")
    assert len(steps) == 1, f"expected one pytest step on the {TEST_JOB} job, found {len(steps)}"
    return skip_gating_env(job, steps[0])


def _measuring_environments() -> list[tuple[str, dict[str, str]]]:
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
    """Every measurement must see the same skip-gating variables the suite runs under.

    Compared in both directions over the whole prefix, so a variable set on
    either side alone is a divergence. That is what makes the gate hold without
    anyone maintaining a roster of which variables gate a skip.
    """
    running = _running_environment()

    for label, measured in _measuring_environments():
        divergent = {
            name: {"test job": running.get(name), label: measured.get(name)}
            for name in sorted(set(running) | set(measured))
            if running.get(name) != measured.get(name)
        }
        assert not divergent, (
            f"the {SURFACE_JOB!r} step {label!r} measures a different environment "
            f"than the {TEST_JOB!r} job runs, on {sorted(divergent)}: {divergent}. A "
            "test gated on one of these would count as running on one side and not "
            "the other, so the comparison would describe a suite nobody runs."
        )


def test_the_environment_comparison_is_not_vacuous() -> None:
    """The comparison must actually see some variables.

    An extractor that returned nothing would make the agreement above hold
    against any pair of jobs, including two that share nothing.
    """
    running = _running_environment()
    assert len(running) >= MIN_COMPARED_ENV, (
        f"the test job carries no {SKIP_GATING_PREFIX}* variables; the agreement "
        "assertion would pass against any workflow"
    )
    for label, measured in _measuring_environments():
        assert len(measured) >= MIN_COMPARED_ENV, (
            f"the {SURFACE_JOB!r} step {label!r} carries no {SKIP_GATING_PREFIX}* variables"
        )


def test_the_surface_job_resolves_a_base_for_every_ci_event() -> None:
    """Each event ci.yml answers needs its own arm naming a base revision.

    The expected set is read off the workflow's own triggers rather than
    restated here. A constant equal to today's triggers passes today and keeps
    passing when a trigger is added: the new event's runs would fall to the
    catch-all, yield no base, skip the comparison, and report success having
    measured nothing — which is the exact outcome this test exists to prevent.
    """
    workflow = _load(CI_WORKFLOW)
    surface = _job(workflow, SURFACE_JOB)
    arms = case_arms(step_script(surface, BASE_STEP_ID))

    missing = sorted(workflow_triggers(workflow) - arms)
    assert not missing, (
        f"the base-resolution step has no arm for {missing}; a {missing} run would "
        "skip the comparison and report success having measured nothing"
    )


def test_the_surface_job_compares_only_when_a_base_resolved() -> None:
    """Every step that reads a measurement must be gated on a base having resolved.

    The whole condition is compared, not searched for the output reference. An
    inverted condition — running the comparison exactly when there is nothing
    to compare — names the same output and would satisfy a containment check,
    which is the input-as-stand-in-for-outcome shape.

    Both the base-emit step and the comparison step are covered. A missing gate
    on the emit fails loudly rather than silently, but it costs nothing to hold
    them to the same condition.
    """
    surface = _job(_load(CI_WORKFLOW), SURFACE_JOB)
    guarded = [
        (str((step or {}).get("name") or "?"), str((step or {}).get("if") or ""))
        for step in surface.get("steps") or []
        if BASE_DEPENDENT_MARKER in str((step or {}).get("run") or "")
    ]
    assert len(guarded) == 2, (
        f"expected the base-emit and comparison steps to read the base, found {len(guarded)}"
    )
    for label, condition in guarded:
        assert condition.strip() == EXPECTED_BASE_CONDITION, (
            f"step {label!r} is not gated on a resolved base: {condition!r} "
            f"(expected {EXPECTED_BASE_CONDITION!r}). A condition naming the output "
            "with the opposite polarity would run this step exactly when there is "
            "nothing to compare."
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
          HF_HUB_OFFLINE: "1"
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
            env:
              SAGE_TEST_DOCKER: "1"
            run: check --emit
    """
)

# Triggers under the boolean key PyYAML produces for a bare ``on:``, and under
# the string key a quoted ``"on":`` produces. Both spellings occur.
_SYNTHETIC_BARE_ON: Final[str] = "on:\n  pull_request:\n  push:\n    branches: [main]\n"
_SYNTHETIC_QUOTED_ON: Final[str] = '"on":\n  workflow_dispatch:\n'

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

    assert "SAGE_TEST_PG_DSN" in running
    assert "SAGE_TEST_PG_DSN" not in measured, "a variable on one side alone must be visible"
    assert "SAGE_TEST_DOCKER" in measured
    assert "SAGE_TEST_DOCKER" not in running, "divergence must be visible in both directions"
    assert running["SAGE_TEST_STUB_PROVIDERS"] == measured["SAGE_TEST_STUB_PROVIDERS"] == "1"
    assert "HF_HUB_OFFLINE" not in running, "the prefix scopes the comparison"


def test_the_trigger_reader_handles_both_spellings_of_the_on_key() -> None:
    """YAML 1.1 turns a bare ``on:`` into the boolean ``True``.

    A reader looking only for the string key finds nothing on every real
    workflow, and the arm gate would then compare against an empty set and pass
    against any step at all.
    """
    assert workflow_triggers(yaml.safe_load(_SYNTHETIC_BARE_ON)) == {"pull_request", "push"}
    assert workflow_triggers(yaml.safe_load(_SYNTHETIC_QUOTED_ON)) == {"workflow_dispatch"}


def test_the_trigger_reader_refuses_a_workflow_with_no_triggers() -> None:
    """An unreadable trigger block halts rather than yielding an empty set."""
    with pytest.raises(AssertionError):
        workflow_triggers(yaml.safe_load("name: x\njobs: {}\n"))


def test_the_arm_parser_reads_alternates_and_drops_the_catch_all() -> None:
    """``a|b)`` counts as two arms; ``*)`` counts as none."""
    assert case_arms(_SYNTHETIC_CASE_BLOCK) == {"pull_request", "merge_group", "push"}


@pytest.mark.parametrize("script", ["", "sha=$PR_BASE_SHA", "echo '*)'"])
def test_the_arm_parser_finds_nothing_without_a_case_block(script: str) -> None:
    """A step that branches on nothing yields no arms, so the gate above bites."""
    assert case_arms(script) == set()
