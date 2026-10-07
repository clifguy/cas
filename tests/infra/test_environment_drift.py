"""Tests for the deployment-environment protection check and its workflow.

Every workflow that signs in to Azure binds a GitHub deployment environment, and
the environment is what the deploy identity's federated credential trusts. Its
protection rules are therefore the gate between a pushed branch and a token the
cloud accepts: a required reviewer, a deployment-branch policy admitting only
``main``, and no administrator bypass. Those rules live in repository settings,
which carry no commit, so only a scheduled read of the live settings can notice
one being removed. ``scripts/check_environment_drift.py`` is that read, run from
the ruleset-drift workflow.

The offline cases build environment payloads in the shape the forge returns
(``GET /repos/{owner}/{repo}/environments`` plus each environment's
``deployment-branch-policies`` listing, which the check folds into the record
under ``branch_policies``). Each defect case flips exactly one field of a
compliant environment, so a check that silently inspects nothing cannot pass
them. The live read is an opt-in tier behind ``SAGE_TEST_LIVE_RULESET=1``, the
same switch the ruleset comparison uses.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from scripts.check_environment_drift import (
    Violation,
    _run,
    check_environment,
    check_environments,
    fetch_live_environments,
    format_report,
    main,
)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
WORKFLOW: Final[Path] = REPO_ROOT / ".github" / "workflows" / "ruleset-drift.yml"
DEPLOYMENT_DOC: Final[Path] = REPO_ROOT / "docs" / "process" / "azure-deployment.md"

LIVE_TIER_ENV: Final[str] = "SAGE_TEST_LIVE_RULESET"

requires_live_environments = pytest.mark.skipif(
    os.environ.get(LIVE_TIER_ENV) != "1" or shutil.which("gh") is None,
    reason=(
        f"live-environment check is opt-in: set {LIVE_TIER_ENV}=1 with an "
        "authenticated gh on PATH (reads the forge API)"
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compliant(name: str = "tenant-prod") -> dict[str, Any]:
    """One environment carrying every required protection, forge-shaped."""
    return {
        "id": 1,
        "node_id": "EN_x",
        "name": name,
        "can_admins_bypass": False,
        "deployment_branch_policy": {
            "protected_branches": False,
            "custom_branch_policies": True,
        },
        "protection_rules": [
            {
                "id": 10,
                "type": "required_reviewers",
                "prevent_self_review": False,
                "reviewers": [{"type": "User", "reviewer": {"login": "owner", "id": 7}}],
            },
            {"id": 11, "type": "branch_policy"},
        ],
        "branch_policies": [{"id": 12, "name": "main", "type": "branch"}],
    }


def _rules(violations: list[Violation]) -> set[str]:
    return {violation.rule for violation in violations}


# ---------------------------------------------------------------------------
# One environment
# ---------------------------------------------------------------------------


def test_compliant_environment_has_no_violations() -> None:
    """E0: the control every defect case below differs from by one field."""
    assert check_environment(_compliant()) == []


def test_missing_reviewer_rule_is_a_violation() -> None:
    """E1: no required-reviewers rule means any branch that may deploy, deploys."""
    env = _compliant()
    env["protection_rules"] = [
        r for r in env["protection_rules"] if r["type"] != "required_reviewers"
    ]

    assert _rules(check_environment(env)) == {"required-reviewer"}


def test_reviewer_rule_with_no_reviewers_is_a_violation() -> None:
    """E2: a rule naming nobody gates nothing, though its type is present."""
    env = _compliant()
    env["protection_rules"][0]["reviewers"] = []

    assert _rules(check_environment(env)) == {"required-reviewer"}


def test_absent_branch_policy_is_a_violation() -> None:
    """E3: a null policy admits every branch, which is the forge default."""
    env = _compliant()
    env["deployment_branch_policy"] = None
    env["branch_policies"] = []

    assert _rules(check_environment(env)) == {"branch-policy"}


def test_protected_branches_mode_is_a_violation() -> None:
    """E4: "protected branches" admits any branch a ruleset protects, not
    ``main`` alone, so it is not the policy this gate requires."""
    env = _compliant()
    env["deployment_branch_policy"] = {"protected_branches": True, "custom_branch_policies": False}
    env["branch_policies"] = []

    assert _rules(check_environment(env)) == {"branch-policy"}

    # The forge never reports both modes at once; a payload that does is not
    # read as the custom policy merely because the ``main`` rule is listed.
    both = _compliant()
    both["deployment_branch_policy"]["protected_branches"] = True

    assert _rules(check_environment(both)) == {"branch-policy"}

    # Nor is a payload with neither mode set, whatever policies it lists.
    neither = _compliant()
    neither["deployment_branch_policy"]["custom_branch_policies"] = False

    assert _rules(check_environment(neither)) == {"branch-policy"}


def test_extra_branch_pattern_is_a_violation() -> None:
    """E5: a second pattern widens the gate even though ``main`` is listed."""
    env = _compliant()
    env["branch_policies"].append({"id": 13, "name": "release/*", "type": "branch"})

    assert _rules(check_environment(env)) == {"branch-policy"}


def test_tag_policy_named_main_is_a_violation() -> None:
    """E6: a tag named ``main`` is not the ``main`` branch."""
    env = _compliant()
    env["branch_policies"] = [{"id": 12, "name": "main", "type": "tag"}]

    assert _rules(check_environment(env)) == {"branch-policy"}


def test_branch_policy_without_a_type_is_a_violation() -> None:
    """E6b: a policy whose type the read did not return is not assumed to be a
    branch policy."""
    env = _compliant()
    del env["branch_policies"][0]["type"]

    assert _rules(check_environment(env)) == {"branch-policy"}


def test_admin_bypass_is_a_violation() -> None:
    """E7: bypass lets an administrator's credential skip the reviewer."""
    env = _compliant()
    env["can_admins_bypass"] = True

    assert _rules(check_environment(env)) == {"admin-bypass"}


def test_absent_admin_bypass_field_is_a_violation() -> None:
    """E8: a field the read did not return is not evidence it is off."""
    env = _compliant()
    del env["can_admins_bypass"]

    assert _rules(check_environment(env)) == {"admin-bypass"}


def test_violation_names_environment_and_live_value() -> None:
    """E9: the report says where to look and what is there now."""
    env = _compliant("tenant-a")
    env["can_admins_bypass"] = True

    (violation,) = check_environment(env)

    assert violation.environment == "tenant-a"
    assert violation.live is True
    assert "tenant-a" in format_report([violation])
    assert "admin-bypass" in format_report([violation])


# ---------------------------------------------------------------------------
# The set of environments
# ---------------------------------------------------------------------------


def test_all_compliant_environments_pass() -> None:
    """S1: the passing shape of the whole check."""
    assert check_environments([_compliant("a"), _compliant("b")]) == []


def test_one_defective_environment_among_compliant_ones_fails() -> None:
    """S2: every environment is checked, not only the first."""
    bad = _compliant("b")
    bad["can_admins_bypass"] = True

    violations = check_environments([_compliant("a"), bad, _compliant("c")])

    assert [(v.environment, v.rule) for v in violations] == [("b", "admin-bypass")]


def test_no_environments_is_a_violation() -> None:
    """S3: an empty listing cannot read as "everything is protected"."""
    assert _rules(check_environments([])) == {"no-environments"}


# ---------------------------------------------------------------------------
# Entry point and I/O edge
# ---------------------------------------------------------------------------


def test_main_exit_codes_from_file(tmp_path: Path) -> None:
    """M1: offline mode exits 0 on compliance and 1 on any violation."""
    good = tmp_path / "good.json"
    good.write_text(json.dumps([_compliant()]), encoding="utf-8")
    bad_env = _compliant()
    bad_env["deployment_branch_policy"] = None
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([bad_env]), encoding="utf-8")

    assert main(["--environments-file", str(good)]) == 0
    assert main(["--environments-file", str(bad)]) == 1


def test_run_raises_on_failed_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """M2: a failed read fails the run instead of reading as compliance."""

    def failing(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args[0], 1, "", "HTTP 401")

    monkeypatch.setattr(subprocess, "run", failing)

    with pytest.raises(RuntimeError, match="HTTP 401"):
        _run(["gh", "api", "x"])


def test_fetch_folds_branch_policies_into_each_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M3: the live read joins each environment to its branch-policy listing,
    and refuses a listing it did not read in full."""
    listing = {"total_count": 1, "environments": [copy.deepcopy(_compliant("tenant"))]}
    del listing["environments"][0]["branch_policies"]
    policies = {"total_count": 1, "branch_policies": [{"id": 1, "name": "main", "type": "branch"}]}
    calls: list[str] = []

    def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(cmd[-1])
        body = policies if cmd[-1].endswith("deployment-branch-policies?per_page=100") else listing
        return subprocess.CompletedProcess(cmd, 0, json.dumps(body), "")

    monkeypatch.setattr("scripts.check_environment_drift._run", fake_run)

    (env,) = fetch_live_environments("owner/repo")

    assert env["branch_policies"] == policies["branch_policies"]
    assert calls == [
        "repos/owner/repo/environments?per_page=100",
        "repos/owner/repo/environments/tenant/deployment-branch-policies?per_page=100",
    ]

    listing["total_count"] = 2
    with pytest.raises(RuntimeError, match="incomplete"):
        fetch_live_environments("owner/repo")

    listing["total_count"] = 1
    policies["total_count"] = 2
    with pytest.raises(RuntimeError, match="incomplete"):
        fetch_live_environments("owner/repo")


# ---------------------------------------------------------------------------
# The gate: the scheduled workflow runs it, the runbook names it
# ---------------------------------------------------------------------------


def test_workflow_runs_the_environment_check_with_a_token() -> None:
    """W1: the scheduled workflow invokes the module with a bound credential,
    read from the parsed step rather than the file's text."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = [
        step
        for job in (workflow.get("jobs") or {}).values()
        for step in (job.get("steps") or [])
        if "scripts.check_environment_drift" in (step.get("run") or "")
    ]

    assert steps, "the scheduled workflow must invoke the environment check"
    assert all(
        "secrets.GITHUB_TOKEN" in (step.get("env") or {}).get("GH_TOKEN", "") for step in steps
    )
    # It runs even when the ruleset comparison before it failed, so one run
    # reports both kinds of drift.
    assert all("!cancelled()" in str(step.get("if") or "") for step in steps)
    assert "id-token" not in (workflow.get("permissions") or {})


def test_runbook_states_the_environment_gate() -> None:
    """W2: the deployment runbook states the three protections the check
    enforces and names the workflow that verifies them."""
    text = DEPLOYMENT_DOC.read_text(encoding="utf-8")

    for phrase in ("required reviewer", "`main`", "bypass", WORKFLOW.name):
        assert phrase in text, f"runbook must mention {phrase!r}"


# ---------------------------------------------------------------------------
# Opt-in tier
# ---------------------------------------------------------------------------


@requires_live_environments
def test_live_environments_are_protected() -> None:
    """L1: the same read the scheduled workflow makes, on demand."""
    environments = fetch_live_environments(os.environ.get("GH_REPO") or None)
    violations = check_environments(environments)

    assert violations == [], format_report(violations)
