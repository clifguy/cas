"""Repo-wide gate: no workflow job is gated on an environment-scoped variable.

GitHub does not expose environment-scoped variables to a *job-level* ``if`` —
only repository/organization variables are in scope there. A job whose ``if``
references ``vars.X`` where ``X`` is environment-scoped therefore evaluates empty
on every run, the job is silently skipped, and the workflow still reports
success (a skipped job does not fail a run). The deploy that "passes" while
nothing is deployed is the worst shape of CI false-green, because the failure is
the *absence* of work rather than an error.

Because the source workflow cannot say whether a given ``vars.X`` is
environment- or repository-scoped (the scoping lives in GitHub settings, not the
YAML), this gate takes the conservative stance the existing deploy-job guard
already takes (``test_deploy_gate_not_keyed_on_environment_scoped_var`` in
``test_infra_deploy_orchestration.py``): no job-level ``if`` references ``vars.``
at all. Jobs are gated by the dispatch/push event, by ``needs`` dependencies, and
by their bound environment instead. That deploy-job guard covers only the single
deploy job in one workflow; this gate is the repo-wide net, so a *different* job
or a *new* workflow cannot reintroduce the skip.

``vars.`` in a *step*-level ``if`` is fine — step scope does see environment
variables — so only job-level ``if`` is inspected. The checks read the tracked
workflow YAML only; they need no GitHub or Azure tooling and run in the ordinary
Python test job. The anti-coincidental controls below prove the detector fires on
the job-level regression and clears the legitimate forms; a "must-not-contain"
scan whose matcher never fired would pass every workflow coincidentally. The
cloud deploy this protects is governed by CAS-ADR-042.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import yaml

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR: Final[Path] = REPO_ROOT / ".github" / "workflows"

# A job-level ``if`` referencing a GitHub Actions variable. Matched against the
# raw condition string, so ``${{ vars.X }}`` and a bare ``vars.X`` both trip.
_VARS_RE: Final[re.Pattern[str]] = re.compile(r"\bvars\.")

# Escape hatch for a future job legitimately gated on a *repository*-scoped
# variable (visible at job-level). Empty today: nothing in the workflows gates a
# job on a variable, and the per-tenant deploy identity is environment-scoped, so
# none may. Entries are ``"<workflow-file>:<job-name>"``.
_ALLOWED: Final[frozenset[str]] = frozenset()

# Workflow files that must be in scope, so a real workflow cannot be silently
# skipped (the blind spot that would make the gate vacuous).
_EXPECTED_WORKFLOWS: Final[tuple[str, ...]] = (
    "infra.yml",
    "ci.yml",
    "build-images.yml",
    "dependabot-triage.yml",
    "sharepoint-validate.yml",
)


# ---------------------------------------------------------------------------
# Detector (pure function over a parsed workflow — exercised by the controls)
# ---------------------------------------------------------------------------


def _job_if_var_offenders(workflow: dict, label: str) -> dict[str, str]:
    """Return ``{"<label>:<job>": condition}`` for each job whose *job-level*
    ``if`` references ``vars.``.

    Only the job-level ``if`` is read; step-level ``if`` (under ``steps``) is
    left untouched, because environment-scoped variables are visible at step
    scope and only the job-level reference is the silent-skip trap.
    """
    offenders: dict[str, str] = {}
    jobs = workflow.get("jobs") if isinstance(workflow, dict) else None
    for name, job in (jobs or {}).items():
        if not isinstance(job, dict):
            continue
        condition = job.get("if")
        if condition is not None and _VARS_RE.search(str(condition)):
            offenders[f"{label}:{name}"] = str(condition)
    return offenders


def _iter_workflow_files() -> list[Path]:
    """Every committed workflow definition under ``.github/workflows/``."""
    return sorted(
        p for p in WORKFLOWS_DIR.iterdir() if p.is_file() and p.suffix in (".yml", ".yaml")
    )


def _all_job_if_var_offenders() -> dict[str, str]:
    """Aggregate :func:`_job_if_var_offenders` across every workflow file."""
    offenders: dict[str, str] = {}
    for path in _iter_workflow_files():
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        offenders.update(_job_if_var_offenders(workflow or {}, path.name))
    return offenders


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_no_job_level_if_references_vars() -> None:
    """No job in any workflow is gated by a job-level ``if`` that references a
    variable. GitHub does not expose environment-scoped variables at job-level, so
    such a gate evaluates empty and silently skips the job while the run stays
    green. Gate jobs by the event, ``needs``, and the bound environment instead.
    """
    offenders = {k: v for k, v in _all_job_if_var_offenders().items() if k not in _ALLOWED}
    assert not offenders, (
        "no job-level `if` may reference `vars.` (an environment-scoped var is "
        "invisible at job-level and silently skips the job while the run reports "
        f"green); gate via the event + needs + environment binding. Offenders: {offenders}"
    )


def test_scan_reaches_all_workflow_files() -> None:
    """The scan visits every expected workflow, so a real workflow cannot be
    silently excluded — the blind spot that would let the gate above pass
    coincidentally.
    """
    scanned = {p.name for p in _iter_workflow_files()}
    missing = [w for w in _EXPECTED_WORKFLOWS if w not in scanned]
    assert not missing, f"workflow gate must scan {missing} (found: {sorted(scanned)})"


# ---------------------------------------------------------------------------
# Anti-coincidental-pass controls
#
# Prove the detector fires on the job-level regression and clears the legitimate
# forms, NOT that the committed workflows happen to be clean. Without these, a
# broken matcher would let the gate pass every workflow vacuously.
# ---------------------------------------------------------------------------


def test_detector_fires_on_job_level_vars_if() -> None:
    """A job-level ``if`` referencing ``vars.`` is flagged — the exact silent-skip
    regression this gate exists to catch.
    """
    workflow = yaml.safe_load(
        "jobs:\n"
        "  deploy:\n"
        "    if: ${{ vars.AZURE_CLIENT_ID != '' }}\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo hi\n"
    )
    offenders = _job_if_var_offenders(workflow, "synthetic.yml")
    assert "synthetic.yml:deploy" in offenders
    assert "vars." in offenders["synthetic.yml:deploy"]


def test_detector_passes_event_and_needs_if() -> None:
    """The two legitimate job-level gate forms in use — the dispatch/push event and
    a ``needs`` output — are not flagged.
    """
    workflow = yaml.safe_load(
        "jobs:\n"
        "  a:\n"
        "    if: github.event_name == 'workflow_dispatch'\n"
        "    runs-on: ubuntu-latest\n"
        "  b:\n"
        "    if: needs.paths-filter.outputs.app == 'true'\n"
        "    runs-on: ubuntu-latest\n"
    )
    assert _job_if_var_offenders(workflow, "synthetic.yml") == {}


def test_detector_ignores_step_level_vars_if() -> None:
    """A ``vars.`` reference in a *step*-level ``if`` is not flagged — step scope
    sees environment variables, so only the job-level reference is the trap.
    """
    workflow = yaml.safe_load(
        "jobs:\n"
        "  a:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - if: ${{ vars.SOMETHING != '' }}\n"
        "        run: echo hi\n"
    )
    assert _job_if_var_offenders(workflow, "synthetic.yml") == {}
