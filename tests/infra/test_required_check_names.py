"""Required status-check contexts are produced by CI alone.

GitHub binds a required context by check-run name, not by workflow. A job in
any other workflow that runs on a pull request and reports under the same name
becomes a second producer of that context: both runs must pass, and the
other workflow's job silently joins the required set. The captured ruleset in
``docs/process/branch_protection.md`` is the authoritative list of required
contexts (``scripts/check_ruleset_drift.py`` holds it to the live ruleset).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.check_ruleset_drift import extract_captured_ruleset

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
CI_WORKFLOW = "ci.yml"

# Events whose check runs land on a pull request's head commit, where the
# required contexts are evaluated. A push lands there too whenever the pushed
# branch is a pull request's head branch, so a push trigger counts unless its
# branch filter admits only protected branches, which are never a head branch.
PR_EVENTS = frozenset({"pull_request", "pull_request_target", "merge_group"})
PROTECTED_BRANCHES = frozenset({"main"})


def _required_contexts() -> set[str]:
    text = (ROOT / "docs" / "process" / "branch_protection.md").read_text(encoding="utf-8")
    ruleset = extract_captured_ruleset(text)
    rule = next(r for r in ruleset["rules"] if r["type"] == "required_status_checks")
    return {check["context"] for check in rule["parameters"]["required_status_checks"]}


def _triggers(workflow: dict) -> dict:
    # PyYAML reads the bare key ``on`` as boolean True.
    on = workflow.get("on", workflow.get(True))
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return dict.fromkeys(on)
    return dict(on or {})


def _push_reaches_pull_requests(config: dict | None) -> bool:
    """A push trigger reaches PR head commits unless it admits only protected branches."""
    branches = (config or {}).get("branches")
    if branches is None or "branches-ignore" in (config or {}):
        return True
    return not set(branches) <= PROTECTED_BRANCHES


def _reaches_pull_requests(workflow: dict) -> bool:
    triggers = _triggers(workflow)
    if set(triggers) & PR_EVENTS:
        return True
    return "push" in triggers and _push_reaches_pull_requests(triggers["push"])


def _check_names(workflow: dict) -> dict[str, str]:
    """Map each top-level job's check-run name to its job key.

    A job that calls a reusable workflow reports as ``<caller> / <callee>``,
    so it cannot collide with a bare required context and is not listed.
    """
    names: dict[str, str] = {}
    for key, job in (workflow.get("jobs") or {}).items():
        if "uses" in job:
            continue
        names[str(job.get("name", key))] = key
    return names


def _pr_workflows() -> list[Path]:
    paths = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        if _reaches_pull_requests(workflow):
            paths.append(path)
    return paths


def test_push_restricted_to_protected_branches_is_exempt() -> None:
    assert not _push_reaches_pull_requests({"branches": ["main"]})
    assert _push_reaches_pull_requests(None)
    assert _push_reaches_pull_requests({"branches": ["main", "release/*"]})
    assert _push_reaches_pull_requests({"branches-ignore": ["gh-pages"]})
    assert _push_reaches_pull_requests({"tags": ["v*"]})


def test_required_contexts_are_known() -> None:
    contexts = _required_contexts()
    assert contexts, "the captured ruleset lists no required status checks"
    ci = yaml.safe_load((WORKFLOWS / CI_WORKFLOW).read_text(encoding="utf-8"))
    assert contexts <= set(_check_names(ci)), "every required context is a CI job"


@pytest.mark.parametrize(
    "path",
    [p for p in _pr_workflows() if p.name != CI_WORKFLOW],
    ids=lambda p: p.name,
)
def test_no_other_pull_request_workflow_reports_a_required_context(path: Path) -> None:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    collisions = {
        name: key for name, key in _check_names(workflow).items() if name in _required_contexts()
    }
    assert not collisions, (
        f"{path.name} reports check runs under required context name(s) {sorted(collisions)} "
        f"(job keys {sorted(collisions.values())}); give the job a distinct `name:`"
    )
